from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from bot.config import config
from bot.models.project import Project, ProjectStatus, db
from bot.services.progress import ProgressTracker

logger = logging.getLogger(__name__)

# System prompt sent to Claude Code to guide app generation
BUILDER_SYSTEM_PROMPT = """You are an expert full-stack developer building a production-ready web application.

CRITICAL REQUIREMENTS:
1. The app MUST listen on port {port} (use environment variable PORT as fallback).
2. Include a complete package.json (or requirements.txt for Python) with ALL dependencies.
3. Include a start script that works with `npm start` or `python app.py`.
4. The app must be SELF-CONTAINED — no external API keys needed unless specified.
5. Use modern, clean, professional design. Not generic — make it look like a real product.
6. Include sample/demo data so the app works immediately without setup.
7. If a database is needed, use SQLite (file-based, no external DB server).
8. Write ALL files needed. Don't skip any file.

APP TYPE: {app_type}

For "fullstack": Build with Node.js (Express/Fastify) + a frontend (React/Vue/vanilla).
For "static" or "landing": Build a static site with HTML/CSS/JS. Include a simple server.
For "dashboard": Build with a charting library, mock data, and professional layout.

PROJECT BRIEF:
{brief}
"""


class Builder:
    """Invokes Claude Code CLI to generate a project from a brief."""

    def __init__(self, project: Project, tracker: ProgressTracker):
        self.project = project
        self.tracker = tracker

    async def build(self) -> bool:
        """Run the full build pipeline. Returns True on success."""
        project_dir = self.project.project_dir
        project_dir.mkdir(parents=True, exist_ok=True)

        try:
            # --- Phase 1: Analyze the brief ---
            await self.tracker.update(
                ProjectStatus.ANALYZING,
                "Understanding your requirements..."
            )

            # --- Phase 2: Generate code with Claude Code ---
            await self.tracker.update(
                ProjectStatus.BUILDING,
                "Writing application code..."
            )
            success = await self._run_claude_code(project_dir)
            if not success:
                return False

            # --- Phase 3: Verify output ---
            await self.tracker.update(
                ProjectStatus.INSTALLING,
                "Checking generated files..."
            )
            if not self._verify_output(project_dir):
                await self.tracker.fail(
                    "Claude Code did not generate the expected files. "
                    "Try a more specific brief."
                )
                return False

            return True

        except Exception as e:
            logger.exception(f"Build failed for {self.project.slug}")
            await self.tracker.fail(str(e))
            return False

    async def _run_claude_code(self, project_dir: Path) -> bool:
        """Invoke claude CLI in non-interactive (print) mode."""
        prompt = BUILDER_SYSTEM_PROMPT.format(
            port=self.project.port,
            app_type=self.project.app_type,
            brief=self.project.brief,
        )

        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", "30",
            "--verbose",
        ]

        # If ANTHROPIC_API_KEY is set, pass it through environment
        env_vars = {}
        if config.ANTHROPIC_API_KEY:
            env_vars["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY

        logger.info(f"Running Claude Code for {self.project.slug}...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_dir),
            env={**dict(__import__("os").environ), **env_vars},
        )

        stdout_chunks = []
        # Stream stdout and update progress periodically
        last_update = 0
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=300  # 5 min timeout per line
                )
            except asyncio.TimeoutError:
                process.kill()
                await self.tracker.fail("Build timed out (5 minutes without output)")
                return False

            if not line:
                break

            decoded = line.decode("utf-8", errors="replace").strip()
            stdout_chunks.append(decoded)

            # Update Telegram every ~10 lines so we don't spam the API
            last_update += 1
            if last_update >= 10:
                last_update = 0
                # Extract a short status hint from recent output
                hint = self._extract_hint(decoded)
                if hint:
                    await self.tracker.update(ProjectStatus.BUILDING, hint)

        await process.wait()
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace")

        full_log = "\n".join(stdout_chunks)
        self.project.build_log = full_log[-5000:]  # Keep last 5k chars
        await db.save(self.project)

        if process.returncode != 0:
            error_msg = stderr[-1000:] if stderr else "Unknown error"
            await self.tracker.fail(f"Claude Code exited with code {process.returncode}:\n{error_msg}")
            return False

        return True

    def _verify_output(self, project_dir: Path) -> bool:
        """Check that Claude Code actually produced files."""
        files = list(project_dir.rglob("*"))
        # Filter out directories, hidden files
        real_files = [
            f for f in files
            if f.is_file() and not f.name.startswith(".")
        ]
        if len(real_files) < 2:
            logger.warning(f"Only {len(real_files)} files generated in {project_dir}")
            return False

        # Check for at least one entry-point-like file
        entry_patterns = [
            "package.json", "index.html", "app.py", "main.py",
            "server.js", "index.js", "app.js",
        ]
        has_entry = any(
            f.name in entry_patterns for f in real_files
        )
        if not has_entry:
            logger.warning(f"No recognizable entry point in {project_dir}")
            # Not fatal — Claude might use a different structure
        return True

    @staticmethod
    def _extract_hint(line: str) -> str:
        """Pull a short user-friendly hint from Claude Code output."""
        # Look for file creation patterns
        file_match = re.search(r"(?:Creating|Writing|Wrote)\s+(.+?)(?:\s|$)", line)
        if file_match:
            return f"Writing {file_match.group(1)}"
        if "installing" in line.lower() or "npm" in line.lower():
            return "Installing packages..."
        if "done" in line.lower() or "complete" in line.lower():
            return "Wrapping up..."
        return ""
