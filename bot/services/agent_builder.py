from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bot.config import config
from bot.models.project import Project, ProjectStatus, db
from bot.services.progress import ProgressTracker
from bot.services.estimator import CostEstimate, INPUT_COST_PER_1M, OUTPUT_COST_PER_1M

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Token tracking
# ──────────────────────────────────────────────

@dataclass
class AgentTokens:
    agent_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    success: bool = False
    error: str = ""

    def calculate_cost(self):
        self.cost_usd = round(
            (self.input_tokens / 1_000_000 * INPUT_COST_PER_1M) +
            (self.output_tokens / 1_000_000 * OUTPUT_COST_PER_1M), 4
        )


@dataclass
class BuildReport:
    agents: list[AgentTokens] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0

    def add(self, agent_tokens: AgentTokens):
        self.agents.append(agent_tokens)
        self.total_input_tokens += agent_tokens.input_tokens
        self.total_output_tokens += agent_tokens.output_tokens
        self.total_cost_usd += agent_tokens.cost_usd
        self.total_duration_seconds += agent_tokens.duration_seconds

    def format_telegram(self) -> str:
        lines = ["📊 <b>Build Report</b>\n"]
        for a in self.agents:
            status = "✅" if a.success else "❌"
            lines.append(
                f"{status} <b>{a.agent_name}</b>: "
                f"{a.input_tokens + a.output_tokens:,} tokens | "
                f"${a.cost_usd:.3f} | "
                f"{a.duration_seconds:.0f}s"
            )
        lines.append(f"\n💰 <b>Total: ${self.total_cost_usd:.2f}</b>")
        lines.append(f"🪙 {self.total_input_tokens + self.total_output_tokens:,} tokens")
        lines.append(f"⏱️ {self.total_duration_seconds:.0f}s total build time")
        return "\n".join(lines)


# ──────────────────────────────────────────────
#  Agent definitions
# ──────────────────────────────────────────────

AGENT_PROMPTS = {
    "architect": """You are the ARCHITECT agent. Your job is to create a detailed technical plan.

WORKING DIRECTORY: {project_dir}
PORT: {port}

Given this project brief, create a PLAN.md file with:
1. Project structure (every file that needs to be created)
2. Tech stack decisions with reasoning
3. Database schema (if needed)
4. API endpoints (if needed)
5. Component breakdown for frontend
6. Data flow description
7. Any third-party libraries needed (with exact package names)

Also create the directory structure with empty placeholder files.

CRITICAL: The app MUST listen on port {port}. Use SQLite for any database needs.
No external API keys — app must be self-contained with demo data.

⚠️ NEVER run `npm start`, `node server.js`, `python app.py`, or any command that starts a server process. Only write files. The app will be started later in Docker.

APP TYPE: {app_type}

BRIEF:
{brief}""",

    "backend": """You are the BACKEND agent. Your job is to implement the server-side code.

WORKING DIRECTORY: {project_dir}
PORT: {port}

Read the PLAN.md file first to understand the architecture.
Then implement ALL backend code:
- Server setup (Express/Fastify for Node.js, or Flask/FastAPI for Python)
- API routes and controllers
- Database setup and models (SQLite)
- Middleware (CORS, error handling, etc.)
- Seed data / demo data
- package.json or requirements.txt with ALL dependencies

The server MUST listen on port {port} (or process.env.PORT).
Include a working `npm start` or `python app.py` command in package.json/scripts.
Make sure all imports and dependencies are correct.
You may run `npm install` to install dependencies, but ⚠️ NEVER run `npm start`, `node server.js`, or any command that starts a long-running server process. Only write code files and install deps.

BRIEF:
{brief}""",

    "database": """You are the DATABASE agent. Your job is to set up the data layer.

WORKING DIRECTORY: {project_dir}
PORT: {port}

Read the PLAN.md file. Focus on:
- SQLite database schema creation
- Migration/seed scripts
- Data models and ORM setup
- Demo/sample data that makes the app look real and populated
- Database utility functions

Create realistic demo data — names, dates, amounts that look like a real app.
All data must be self-contained (no external dependencies).

⚠️ NEVER run `npm start`, `node server.js`, `python app.py`, or any command that starts a server process. Only write files and run setup scripts.

BRIEF:
{brief}""",

    "frontend": """You are the FRONTEND agent. Your job is to build a stunning, pixel-perfect UI.

WORKING DIRECTORY: {project_dir}
PORT: {port}

Read the PLAN.md file. Build ALL frontend code:
- Beautiful, modern, PROFESSIONAL design
- NOT generic — this should look like a real product, not a template
- Responsive layout that works on mobile and desktop
- Smooth animations and micro-interactions
- Proper typography with good font choices (Google Fonts)
- A cohesive color palette that fits the brand/purpose
- Loading states, empty states, error states
- Icons (use Lucide, Heroicons, or similar CDN-based icons)

If React/Vue: Set up the full component tree with routing.
If vanilla: Create clean HTML/CSS/JS with modern ES6+.

Make it look AMAZING. Not generic Bootstrap. Not default Tailwind.
Custom, thoughtful, pixel-perfect design.

⚠️ NEVER run `npm start`, `node server.js`, or any command that starts a server process. Only write code files.

BRIEF:
{brief}""",

    "integrator": """You are the INTEGRATOR agent. Your job is to wire everything together and fix issues.

WORKING DIRECTORY: {project_dir}
PORT: {port}

Review ALL files in the project. Your tasks:
1. Make sure frontend connects to backend API correctly
2. Fix any import/require path issues
3. Ensure all dependencies are in package.json / requirements.txt
4. Fix any TypeScript/ESLint errors
5. Make sure static files are served correctly
6. Run `npm install` if package.json exists to verify deps install cleanly
7. Fix any missing files referenced in code
8. Verify server.js/app.js reads PORT from environment: process.env.PORT || {port}

If you find issues, FIX them. Don't just report — fix the actual code.

⚠️ CRITICAL: NEVER run `npm start`, `node server.js`, `python app.py`, or ANY command that starts a long-running server. The app will be started in Docker later. You may only run short commands like `npm install`, `node -e "..."` for syntax checks, etc.

BRIEF:
{brief}""",

    "qa": """You are the QA agent. Your job is to review and polish the code.

WORKING DIRECTORY: {project_dir}
PORT: {port}

Final review pass. Check by READING the code (do NOT start the server):
1. Does package.json have a valid "start" script?
2. Does server.js/app.js use process.env.PORT || {port}?
3. Are all imported modules listed in package.json dependencies?
4. Is the HTML/CSS/JS well-formed and complete?
5. Is the design polished? Fix any visual issues in the code.
6. Is the demo data realistic and complete?
7. Are there any broken references or missing files?
8. Does the responsive design CSS look correct?

Fix any issues you find by editing the files directly.
The goal: production-quality code that will work when started in Docker.

⚠️ CRITICAL: NEVER run `npm start`, `node server.js`, `python app.py`, or ANY command that starts a long-running server. Only read files, review code, and edit files to fix issues.

BRIEF:
{brief}""",
}


# ──────────────────────────────────────────────
#  Checkpoint system
# ──────────────────────────────────────────────

CHECKPOINT_FILE = ".appfactory_checkpoint.json"


def save_checkpoint(project_dir: Path, agent_name: str, status: str, report: BuildReport):
    """Save build progress so we can resume on failure."""
    checkpoint = {
        "last_completed_agent": agent_name,
        "status": status,
        "timestamp": time.time(),
        "report": {
            "total_input_tokens": report.total_input_tokens,
            "total_output_tokens": report.total_output_tokens,
            "total_cost_usd": report.total_cost_usd,
            "agents_completed": [a.agent_name for a in report.agents if a.success],
        },
    }
    checkpoint_path = project_dir / CHECKPOINT_FILE
    checkpoint_path.write_text(json.dumps(checkpoint, indent=2))


def load_checkpoint(project_dir: Path) -> Optional[dict]:
    """Load the last checkpoint if it exists."""
    checkpoint_path = project_dir / CHECKPOINT_FILE
    if checkpoint_path.exists():
        try:
            return json.loads(checkpoint_path.read_text())
        except Exception:
            return None
    return None


# ──────────────────────────────────────────────
#  Multi-Agent Builder
# ──────────────────────────────────────────────

class MultiAgentBuilder:
    """
    Orchestrates multiple specialist agents to build a project.
    Each agent runs Claude Code with a focused prompt.
    Checkpoints after each agent so failed builds can resume.
    Tracks token usage per agent for cost reporting.
    """

    def __init__(self, project: Project, tracker: ProgressTracker,
                 estimate: CostEstimate):
        self.project = project
        self.tracker = tracker
        self.estimate = estimate
        self.report = BuildReport()

    async def build(self) -> bool:
        """Run the full multi-agent build pipeline."""
        project_dir = self.project.project_dir
        project_dir.mkdir(parents=True, exist_ok=True)

        agents_to_run = list(self.estimate.agents_needed)

        # Check for checkpoint — resume if previous build partially succeeded
        checkpoint = load_checkpoint(project_dir)
        skipped_agents = set()
        if checkpoint and checkpoint.get("status") == "partial":
            completed = set(checkpoint.get("report", {}).get("agents_completed", []))
            agents_to_run = [a for a in agents_to_run if a not in completed]
            skipped_agents = completed
            if agents_to_run:
                self.tracker.log(f"♻️ Resuming: skipping {', '.join(completed)}")
                logger.info(f"Resuming {self.project.slug}: skipping {completed}")

        # Mark skipped agents
        for agent_name in skipped_agents:
            await self.tracker.step_skip(f"agent:{agent_name}", "Completed in previous run")

        for agent_name in agents_to_run:
            step_key = f"agent:{agent_name}"
            await self.tracker.step_start(step_key, "Running...")

            agent_tokens = await self._run_agent(agent_name, project_dir)
            self.report.add(agent_tokens)

            if not agent_tokens.success:
                # Save checkpoint so we can resume later
                save_checkpoint(project_dir, agent_name, "partial", self.report)

                # Try one retry with error context
                await self.tracker.step_start(step_key, "Retrying with error fix...")
                self.tracker.log(f"⚠️ {agent_name} failed: {agent_tokens.error[:200]}")

                retry_tokens = await self._run_agent(
                    agent_name, project_dir,
                    extra_context=f"\n\nPREVIOUS ATTEMPT FAILED WITH:\n{agent_tokens.error}\n\nFix the issues and try again."
                )
                self.report.add(retry_tokens)

                if not retry_tokens.success:
                    save_checkpoint(project_dir, agent_name, "failed", self.report)
                    error_msg = (
                        f"Agent '{agent_name}' failed after retry.\n"
                        f"Error: {retry_tokens.error[:300]}\n\n"
                        f"Use /rebuild to try again (will resume from checkpoint)."
                    )
                    await self.tracker.step_fail(step_key, retry_tokens.error[:100])
                    await self.tracker.fail(error_msg)
                    return False

            # Agent succeeded (possibly on retry)
            cost_str = f"${agent_tokens.cost_usd:.3f}" if agent_tokens.cost_usd else ""
            await self.tracker.step_done(
                step_key,
                f"Done in {agent_tokens.duration_seconds:.0f}s {cost_str}".strip()
            )

            # Checkpoint after each successful agent
            save_checkpoint(project_dir, agent_name, "partial", self.report)

        # All agents done
        save_checkpoint(project_dir, "all", "complete", self.report)
        return True

    async def _run_agent(self, agent_name: str, project_dir: Path,
                         extra_context: str = "") -> AgentTokens:
        """Run a single specialist agent via Claude Code."""
        tokens = AgentTokens(agent_name=agent_name)
        start_time = time.time()

        prompt_template = AGENT_PROMPTS.get(agent_name)
        if not prompt_template:
            tokens.error = f"Unknown agent: {agent_name}"
            return tokens

        prompt = prompt_template.format(
            project_dir=str(project_dir),
            port=self.project.port,
            app_type=self.project.app_type,
            brief=self.project.brief,
        )

        if extra_context:
            prompt += extra_context

        # Determine max turns based on agent type
        agent_max_turns = {
            "architect": 15,
            "backend": 25,
            "database": 15,
            "frontend": 25,
            "integrator": 20,
            "qa": 15,
        }
        max_turns = agent_max_turns.get(agent_name, 20)

        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", str(max_turns),
            "--output-format", "stream-json",
        ]

        env_vars = dict(os.environ)
        if config.ANTHROPIC_API_KEY:
            env_vars["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY

        self.tracker.log(f"🤖 Agent '{agent_name}' starting (max {max_turns} turns)...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_dir),
                env=env_vars,
            )

            # Stream stdout for live progress updates
            stdout_chunks = []
            last_update_time = 0
            tool_count = 0
            files_written = set()

            try:
                async def read_stream():
                    nonlocal last_update_time, tool_count
                    while True:
                        line = await asyncio.wait_for(
                            process.stdout.readline(), timeout=660
                        )
                        if not line:
                            break
                        stdout_chunks.append(line)

                        # Try to parse each line as JSON for streaming updates
                        try:
                            event = json.loads(line.decode("utf-8", errors="replace"))
                            event_type = event.get("type", "")

                            # Track tool use for progress
                            if event_type == "assistant" and "message" in event:
                                msg = event["message"]
                                if isinstance(msg, dict):
                                    for block in msg.get("content", []):
                                        if isinstance(block, dict):
                                            if block.get("type") == "tool_use":
                                                tool_count += 1
                                                tool_name = block.get("name", "")
                                                if tool_name in ("Write", "Edit"):
                                                    inp = block.get("input", {})
                                                    fpath = inp.get("file_path", "")
                                                    if fpath:
                                                        fname = Path(fpath).name
                                                        files_written.add(fname)

                            # Update progress periodically (max every 5s)
                            now = time.time()
                            if now - last_update_time >= 5:
                                last_update_time = now
                                detail = f"{tool_count} actions"
                                if files_written:
                                    recent = list(files_written)[-3:]
                                    detail += f" | {', '.join(recent)}"
                                step_key = f"agent:{agent_name}"
                                step = self.tracker.get_step(step_key)
                                if step and step.status == "running":
                                    step.detail = detail
                                    await self.tracker._update_message()

                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass

                await asyncio.wait_for(read_stream(), timeout=660)
                stderr_bytes = await process.stderr.read()
                await process.wait()

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                tokens.error = f"Agent '{agent_name}' timed out after 10 minutes"
                tokens.duration_seconds = time.time() - start_time
                self.tracker.log(f"⏱️ Agent '{agent_name}' timed out")
                return tokens

            stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            tokens.duration_seconds = time.time() - start_time

            # Parse token usage from stream-json output
            # The last JSON line with "result" has the final usage stats
            for line in reversed(stdout.strip().split("\n")):
                try:
                    data = json.loads(line)
                    # Check for result message with usage
                    if data.get("type") == "result":
                        usage = data.get("usage", {})
                        tokens.input_tokens = usage.get("input_tokens", 0)
                        tokens.output_tokens = usage.get("output_tokens", 0)
                        break
                    # Also check nested structures
                    if "usage" in data:
                        usage = data["usage"]
                        if usage.get("input_tokens"):
                            tokens.input_tokens = usage["input_tokens"]
                            tokens.output_tokens = usage.get("output_tokens", 0)
                            break
                except (json.JSONDecodeError, KeyError):
                    continue

            if not tokens.input_tokens:
                # Fallback: try regex on stderr
                token_match = re.search(r"(\d+)\s*input.*?(\d+)\s*output", stderr)
                if token_match:
                    tokens.input_tokens = int(token_match.group(1))
                    tokens.output_tokens = int(token_match.group(2))
                else:
                    # Rough estimate based on output length
                    tokens.output_tokens = len(stdout) // 4
                    tokens.input_tokens = len(prompt) // 4

            tokens.calculate_cost()

            if process.returncode != 0:
                tokens.error = stderr[-500:] if stderr else f"Exit code {process.returncode}"
                tokens.success = False
            else:
                tokens.success = True

            self.tracker.log(
                f"🤖 Agent '{agent_name}' finished: "
                f"{'✓' if tokens.success else '✗'} | "
                f"{tokens.input_tokens + tokens.output_tokens:,} tokens | "
                f"${tokens.cost_usd:.3f} | "
                f"{tokens.duration_seconds:.0f}s | "
                f"{tool_count} tool calls | "
                f"{len(files_written)} files touched"
            )

        except Exception as e:
            tokens.error = str(e)
            tokens.duration_seconds = time.time() - start_time
            self.tracker.log(f"💥 Agent '{agent_name}' crashed: {e}")
            logger.exception(f"Agent '{agent_name}' crashed")

        return tokens

    def get_report(self) -> BuildReport:
        return self.report
