"""docker-compose support for IMPORTED multi-service repos.

Some imported projects (a separate backend + frontend, sometimes more services)
ship their own `docker-compose.yml`. Instead of forcing them into the bot's
single-container model, we deploy them exactly as the repo defines: bring the
whole stack up with `docker compose up -d --build`, then route the public
subdomain to whichever service publishes a web port.

Everything is namespaced under the compose project name `appfactory-<slug>` so
multiple imported stacks coexist without colliding. The repo's compose file is
treated as the source of truth — we never rewrite it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import yaml

from bot.models.project import Project
from bot.services.progress import ProgressTracker

logger = logging.getLogger(__name__)

# Where a compose file might live, in priority order.
COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
COMPOSE_SEARCH_DIRS = (".", "infra", "deploy", "docker", ".docker", "ops")

# Services that look like databases / infrastructure — never the public web entry.
_DB_IMAGE_RE = re.compile(
    r"(postgres|pgvector|mysql|mariadb|mongo|redis|rabbitmq|memcached|"
    r"elastic|opensearch|clickhouse|cassandra|kafka|zookeeper|nats|etcd|"
    r"minio|vault|consul)",
    re.IGNORECASE,
)
_DB_PORTS = {5432, 3306, 6379, 27017, 5672, 15672, 11211, 9200, 9300, 9092, 2181, 1521, 1433}

# How long a (possibly multi-service) compose build+up may take.
COMPOSE_UP_TIMEOUT = 45 * 60


def find_compose_file(project_dir: Path) -> str | None:
    """Return the repo-relative path of a compose file, or None. Root wins."""
    for d in COMPOSE_SEARCH_DIRS:
        for fn in COMPOSE_FILENAMES:
            rel = fn if d == "." else f"{d}/{fn}"
            if (project_dir / rel).is_file():
                return rel
    return None


def _container_ports(svc: dict) -> list[int]:
    """Extract the container-side (target) ports a service declares under `ports`."""
    out: list[int] = []
    for entry in svc.get("ports", []) or []:
        try:
            if isinstance(entry, dict):  # long syntax {target, published, ...}
                tgt = entry.get("target")
                if tgt is not None:
                    out.append(int(tgt))
                continue
            # short syntax: "[ip:][host:]container[/proto]", ranges "a-b"
            spec = str(entry).split("/", 1)[0]
            container = spec.split(":")[-1]
            out.append(int(container.split("-")[0]))
        except (ValueError, TypeError):
            continue
    return out


def parse_services(compose_path: Path) -> dict:
    """Parse a compose file into {service_name: service_dict}. {} on error."""
    try:
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8", errors="replace")) or {}
    except Exception:
        logger.exception("Failed to parse compose file %s", compose_path)
        return {}
    services = data.get("services")
    return services if isinstance(services, dict) else {}


def web_service_candidates(services: dict) -> list[tuple[str, int]]:
    """Services that publish a non-DB web port → [(service_name, container_port)]."""
    candidates: list[tuple[str, int]] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if _DB_IMAGE_RE.search(str(svc.get("image", ""))):
            continue
        web_ports = [p for p in _container_ports(svc) if p not in _DB_PORTS]
        if web_ports:
            candidates.append((name, web_ports[0]))
    return candidates


class ComposeManager:
    """Runs an imported project's own docker-compose stack."""

    def __init__(self, project: Project, tracker: ProgressTracker):
        self.project = project
        self.tracker = tracker

    @property
    def project_name(self) -> str:
        return f"appfactory-{self.project.slug}"

    @property
    def compose_path(self) -> Path:
        return self.project.project_dir / self.project.compose_file

    def _base(self) -> list[str]:
        return ["docker", "compose", "-p", self.project_name, "-f", str(self.compose_path)]

    # ── Lifecycle ────────────────────────────
    async def up(self) -> tuple[bool, str]:
        """Build and start the whole stack (idempotent — recreates changed services)."""
        return await self._run(
            self._base() + ["up", "-d", "--build", "--remove-orphans"],
            timeout=COMPOSE_UP_TIMEOUT,
        )

    async def down(self, volumes: bool = False, rmi: bool = False) -> tuple[bool, str]:
        cmd = self._base() + ["down", "--remove-orphans"]
        if volumes:
            cmd.append("-v")
        if rmi:
            cmd += ["--rmi", "local"]
        return await self._run(cmd, timeout=300)

    async def discover_published_port(self) -> int | None:
        """Ask compose what host port the web service's container port is published on."""
        ok, out = await self._run(
            self._base() + ["port", self.project.web_service, str(self.project.web_container_port)],
            timeout=60, quiet=True,
        )
        # Output looks like "0.0.0.0:49153" / "127.0.0.1:8080" / ":::8080".
        m = re.search(r":(\d+)\s*$", out.strip())
        if m:
            return int(m.group(1))
        return None

    async def is_running(self) -> bool:
        ok, out = await self._run(
            self._base() + ["ps", "-q", "--status", "running"], timeout=60, quiet=True,
        )
        return ok and bool(out.strip())

    async def logs(self, tail: int = 40, service: str | None = None) -> str:
        cmd = self._base() + ["logs", "--no-color", "--tail", str(tail)]
        if service:
            cmd.append(service)
        _, out = await self._run(cmd, timeout=60, quiet=True)
        return out or "No compose logs available."

    # ── Health ───────────────────────────────
    async def await_health(self, port: int) -> bool:
        """A server answering on the routed port (incl. 4xx) counts as up; 5xx/000 don't."""
        await self.tracker.step_start("health_check", f"Waiting for port {port}...")
        await asyncio.sleep(3)
        detail = ""
        for attempt in range(12):
            code = await self._http_code(port)
            if code and code[0] in ("2", "3", "4"):
                self.tracker.log(f"Health check passed: HTTP {code}")
                await self.tracker.step_done("health_check", f"HTTP {code} OK")
                return True
            detail = "Connection refused" if code == "000" else f"HTTP {code}"
            step = self.tracker.get_step("health_check")
            if step:
                step.detail = f"Attempt {attempt + 1}/12: {detail}"
            if attempt < 11:
                await asyncio.sleep(4)
        self.tracker.log(f"Health check did not pass: {detail}")
        return False

    async def _http_code(self, port: int) -> str:
        try:
            _, out = await self._run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--connect-timeout", "3", "--max-time", "5", f"http://127.0.0.1:{port}/"],
                timeout=10, quiet=True,
            )
            return out.strip()
        except Exception:
            return "000"

    # ── Subprocess helper ────────────────────
    async def _run(self, cmd: list[str], timeout: int, quiet: bool = False) -> tuple[bool, str]:
        if not quiet:
            self.tracker.log(f"$ {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.project.project_dir),
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False, f"Command timed out after {timeout // 60} minutes."
            out = stdout.decode("utf-8", errors="replace")
            return proc.returncode == 0, out
        except FileNotFoundError as e:
            return False, f"Command not found: {e}"
        except Exception as e:  # pragma: no cover
            logger.exception("compose command failed")
            return False, str(e)
