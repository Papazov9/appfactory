"""Git / GitHub integration.

Once `/repo` is run for a project, the project directory on the server becomes a
working tree that tracks a private GitHub repo — the single source of truth.
From then on the only sanctioned ways to change a deployed app are:
  - `/update` (AI edits, which auto-commit & push — Phase 6), or
  - the developer pushing to GitHub and running `/redeploy`.

Authentication: the bot pushes/fetches over HTTPS using GITHUB_TOKEN embedded in
the `origin` remote. The clean https URL (no token) is stored on the project for
the user to clone with their own credentials.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from bot.config import config
from bot.models.project import Project, db
from bot.services.progress import ProgressTracker

logger = logging.getLogger(__name__)


GITIGNORE = """# Dependencies
node_modules/
**/node_modules/

# Build output
target/
**/target/
dist/
**/dist/
build/

# AppFactory internals
.appfactory_checkpoint.json
.appfactory/

# Runtime data (lives in Docker volumes, not in source control)
data/
*.db
*.sqlite
*.sqlite3

# Env / secrets
.env
.env.*

# OS / editor
.DS_Store
Thumbs.db
"""


class GitError(Exception):
    pass


class GitManager:
    """Per-project git operations + GitHub repo creation."""

    def __init__(self, project: Project, tracker: ProgressTracker | None = None):
        self.project = project
        self.tracker = tracker
        self.dir = str(project.project_dir)

    def _log(self, msg: str):
        if self.tracker:
            self.tracker.log(msg)
        logger.info(f"[{self.project.slug}] {msg}")

    # ── git plumbing ─────────────────────────
    async def _git(self, *args: str, check: bool = True) -> tuple[int, str]:
        """Run a git command inside the project dir. Returns (returncode, output)."""
        cmd = [
            "git",
            "-c", f"user.name={config.GIT_AUTHOR_NAME}",
            "-c", f"user.email={config.GIT_AUTHOR_EMAIL}",
            "-C", self.dir,
            *args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = (stdout + stderr).decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} failed: {out[-400:]}")
        return proc.returncode, out

    def _token_remote(self, full_name: str) -> str:
        return f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{full_name}.git"

    async def _has_git(self) -> bool:
        rc, _ = await self._git("rev-parse", "--is-inside-work-tree", check=False)
        return rc == 0

    def _write_gitignore(self):
        gi = self.project.project_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)

    # ── GitHub API ───────────────────────────
    async def _create_remote_repo(self) -> dict:
        """Create a private GitHub repo for this project under the token's account."""
        name = self.project.slug
        payload = {
            "name": name,
            "private": True,
            "description": f"AppFactory project: {self.project.name}",
            "auto_init": False,
        }
        headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{config.GITHUB_API_BASE}/user/repos", json=payload, headers=headers
            )
            if resp.status_code == 422:
                # Name already taken — retry once with the project id suffixed.
                payload["name"] = f"{name}-{self.project.id}"
                resp = await client.post(
                    f"{config.GITHUB_API_BASE}/user/repos", json=payload, headers=headers
                )
            if resp.status_code not in (200, 201):
                raise GitError(f"GitHub repo creation failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()

    # ── Public operations ────────────────────
    async def create_repo(self) -> str:
        """Init git, commit the source, create the GitHub repo, push. Returns the html URL."""
        if not config.github_enabled:
            raise GitError("GitHub is not configured. Set GITHUB_TOKEN and GITHUB_OWNER in .env.")
        if not self.project.project_dir.exists():
            raise GitError(f"Project directory not found: {self.dir}")

        self._write_gitignore()

        if not await self._has_git():
            await self._git("init")
        await self._git("add", "-A")
        # Commit (allow empty so a re-run doesn't hard-fail).
        rc, out = await self._git(
            "commit", "-m", "Initial commit from AppFactory", check=False
        )
        if rc != 0 and "nothing to commit" not in out:
            self._log(f"git commit note: {out[-200:]}")

        repo = await self._create_remote_repo()
        full_name = repo["full_name"]
        html_url = repo["html_url"]
        branch = repo.get("default_branch") or "main"

        await self._git("branch", "-M", branch)
        # (Re)point origin at the token-authenticated URL.
        rc, _ = await self._git("remote", "get-url", "origin", check=False)
        if rc == 0:
            await self._git("remote", "set-url", "origin", self._token_remote(full_name))
        else:
            await self._git("remote", "add", "origin", self._token_remote(full_name))

        self._log(f"Pushing to {html_url} ...")
        await self._git("push", "-u", "origin", branch)

        self.project.repo_url = html_url
        self.project.repo_full_name = full_name
        self.project.default_branch = branch
        await db.save(self.project)
        return html_url

    async def pull_latest(self) -> str:
        """Fetch and hard-reset the working tree to origin's latest. Returns the short SHA."""
        if not self.project.repo_full_name:
            raise GitError("This project has no GitHub repo yet — run /repo first.")
        branch = self.project.default_branch or "main"
        # Make sure the remote carries the token (it may have been created in an
        # older session, or the token may have been refreshed).
        await self._git("remote", "set-url", "origin",
                        self._token_remote(self.project.repo_full_name), check=False)
        self._log(f"Fetching origin/{branch} ...")
        await self._git("fetch", "origin", branch)
        await self._git("reset", "--hard", f"origin/{branch}")
        _, sha = await self._git("rev-parse", "--short", "HEAD", check=False)
        return sha.strip()

    async def commit_and_push(self, message: str) -> bool:
        """Commit any working-tree changes and push (used by AI /update). No-op if clean."""
        if not self.project.repo_full_name:
            return False  # not under git yet — nothing to sync
        branch = self.project.default_branch or "main"
        await self._git("add", "-A")
        rc, out = await self._git("commit", "-m", message, check=False)
        if rc != 0:
            if "nothing to commit" in out:
                return False
            self._log(f"git commit failed: {out[-200:]}")
            return False
        await self._git("remote", "set-url", "origin",
                        self._token_remote(self.project.repo_full_name), check=False)
        rc, out = await self._git("push", "origin", branch, check=False)
        if rc != 0:
            self._log(f"git push failed: {out[-200:]}")
            return False
        return True
