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
from bot.services import stacks

logger = logging.getLogger(__name__)

# The `claude` CLI emits one JSON object per line in stream-json mode. A single
# line (e.g. a big PLAN.md Write or a large file Read echoed back) can far exceed
# asyncio's default 64 KB StreamReader limit, which would raise
# "Separator is found, but chunk is longer than limit" and kill the agent.
# Give the subprocess streams a generous 64 MB line buffer instead.
STREAM_BUFFER_LIMIT = 64 * 1024 * 1024

# How long an agent may stay COMPLETELY SILENT (no stream-json output at all)
# before we treat it as hung and kill it. This must exceed the longest single
# tool call an agent realistically makes — e.g. a full `mvn package`, `npm
# install`, or test suite that prints nothing to our JSON stream until it
# finishes. 25 minutes covers the slowest realistic build step. This is the
# real hang detector; the per-agent wall-clock cap below is just a ceiling.
AGENT_IDLE_TIMEOUT = 25 * 60

# Per-agent wall-clock ceilings (seconds) BEFORE complexity scaling. A complex
# or massive brief legitimately needs long steps, so these bases are multiplied
# by COMPLEXITY_TIME_MULTIPLIER at runtime. Frontend/backend/integrator do the
# heaviest work and get the largest bases.
BASE_AGENT_TIMEOUTS = {
    "architect": 900,    # 15 min
    "backend": 1500,     # 25 min
    "database": 900,     # 15 min
    "frontend": 1500,    # 25 min
    "integrator": 1500,  # 25 min
    "qa": 900,           # 15 min
}

# Bigger projects get proportionally more wall-clock time per agent. A `complex`
# backend step thus gets 1500 * 2.0 = 50 min, `massive` gets 75 min, before the
# idle hang-detector or max-turns cap it.
COMPLEXITY_TIME_MULTIPLIER = {
    "trivial": 1.0,
    "simple": 1.0,
    "moderate": 1.5,
    "complex": 2.0,
    "massive": 3.0,
}


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
    session_id: str = ""  # Claude Code session id (for cached-context updates)
    files_written: list = field(default_factory=list)  # files this agent created/edited

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
    last_session_id: str = ""  # most recent Claude session id seen

    def add(self, agent_tokens: AgentTokens):
        self.agents.append(agent_tokens)
        self.total_input_tokens += agent_tokens.input_tokens
        self.total_output_tokens += agent_tokens.output_tokens
        self.total_cost_usd += agent_tokens.cost_usd
        self.total_duration_seconds += agent_tokens.duration_seconds
        if agent_tokens.session_id:
            self.last_session_id = agent_tokens.session_id

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
    "architect": """You are the ARCHITECT agent. Create a detailed technical plan.

WORKING DIRECTORY: {project_dir}
PORT: {port}
STACK: {stack}

Given this brief, create a PLAN.md file with:
1. Project structure — every file to create, in the idiomatic layout for this stack
2. Library/dependency choices with exact package names and versions
3. Data model / DB schema (only if the app needs persistence)
4. API endpoints (if any)
5. Frontend component breakdown (if any)
6. Data flow

Also create the directory structure with placeholder files.
The app must be self-contained: no external API keys, realistic demo data.
Follow the STACK RULES below exactly.

BRIEF:
{brief}""",

    "backend": """You are the BACKEND agent. Implement the server-side code.

WORKING DIRECTORY: {project_dir}
PORT: {port}
STACK: {stack}

Read PLAN.md first. Then implement ALL backend code per the STACK RULES below:
- App/server setup and the entry point
- Routes/controllers and business logic
- Data models & persistence (only if the app needs it)
- The dependency manifest with EVERY dependency pinned
- Demo/seed data so the app looks real and populated

You may install dependencies and run short syntax checks, but NEVER start a
long-running server process — only write code files.

BRIEF:
{brief}""",

    "database": """You are the DATABASE agent. Set up the data layer.

WORKING DIRECTORY: {project_dir}
PORT: {port}
STACK: {stack}

Read PLAN.md. Per the STACK RULES below, focus on:
- Schema / entities / migrations
- ORM or data-access setup
- Realistic seed/demo data (names, dates, amounts that look like a real app)
- Data-access helper functions

All data must be self-contained. NEVER start a server process — only write
files and run short setup/migration scripts.

BRIEF:
{brief}""",

    "frontend": """You are the FRONTEND agent. Build a stunning, professional UI.

WORKING DIRECTORY: {project_dir}
PORT: {port}
STACK: {stack}

Read PLAN.md. Build ALL frontend code in the stack's conventional structure:
- Modern, PROFESSIONAL design — looks like a real product, not a template
- Responsive across mobile and desktop
- Smooth interactions, good typography, a cohesive palette
- Loading, empty, and error states
- Tasteful icons

Make it look AMAZING — custom and thoughtful, not generic Bootstrap.
NEVER start a dev server — only write code files.

BRIEF:
{brief}""",

    "integrator": """You are the INTEGRATOR agent. Wire everything together and fix issues.

WORKING DIRECTORY: {project_dir}
PORT: {port}
STACK: {stack}

Review ALL files. Per the STACK RULES below:
1. Ensure the frontend talks to the backend correctly (same-origin /api)
2. Fix import/path/build issues
3. Ensure every dependency is declared and the project builds cleanly
4. Ensure the entry point reads PORT from the environment
5. Fix any missing/broken references

FIX issues directly — don't just report them. You may run short
build/install/syntax-check commands, but NEVER start a long-running server.

BRIEF:
{brief}""",

    "qa": """You are the QA agent. Review and polish by READING the code (do NOT start the server).

WORKING DIRECTORY: {project_dir}
PORT: {port}
STACK: {stack}

Final pass, per the STACK RULES below:
1. The project builds and its entry point binds to PORT (from env), on 0.0.0.0
2. Every imported module is in the dependency manifest
3. Code is complete and well-formed, with no broken references
4. The design is polished; demo data is realistic and complete
5. Fix any issues by editing the files directly

Goal: production-quality code that works when started in Docker.
NEVER start a long-running server — only read and edit files.

BRIEF:
{brief}""",
}


# ──────────────────────────────────────────────
#  Checkpoint system
# ──────────────────────────────────────────────

CHECKPOINT_FILE = ".appfactory_checkpoint.json"


def save_checkpoint(project_dir: Path, agent_name: str, status: str, report: BuildReport,
                    completed_agents: Optional[set] = None,
                    agent_progress: Optional[dict] = None):
    """Save build progress so we can resume on failure.

    `completed_agents` is the CUMULATIVE set of agents finished across all runs
    (including ones skipped via a previous checkpoint). It must be passed
    explicitly — deriving it from `report.agents` alone loses agents that were
    skipped on resume, since this run's report only holds agents that actually
    ran this time. The fallback below preserves old behaviour for any caller
    that doesn't supply it.

    `agent_progress` is per-agent breadcrumbs (files written, last error,
    session id) for EVERY agent that has run — including failed/in-progress
    ones — so a resumed agent can continue from its partial work instead of
    regenerating it.
    """
    if completed_agents is None:
        completed_agents = [a.agent_name for a in report.agents if a.success]
    checkpoint = {
        "last_completed_agent": agent_name,
        "status": status,
        "timestamp": time.time(),
        "report": {
            "total_input_tokens": report.total_input_tokens,
            "total_output_tokens": report.total_output_tokens,
            "total_cost_usd": report.total_cost_usd,
            "agents_completed": sorted(set(completed_agents)),
        },
        "agent_progress": agent_progress or {},
    }
    # Write atomically (temp file + replace) so a crash mid-write can't leave a
    # corrupt checkpoint, and never let a checkpoint write error kill the build.
    try:
        checkpoint_path = project_dir / CHECKPOINT_FILE
        tmp_path = checkpoint_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(checkpoint, indent=2))
        tmp_path.replace(checkpoint_path)
    except Exception:
        logger.exception("Failed to write checkpoint (continuing build)")


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
        # Per-agent breadcrumbs from prior runs (files already written, last error,
        # session id) so a resumed/retried agent continues instead of redoing work.
        agent_progress: dict = {}
        if checkpoint and checkpoint.get("status") in ("partial", "failed"):
            completed = set(checkpoint.get("report", {}).get("agents_completed", []))
            agent_progress = dict(checkpoint.get("agent_progress", {}))
            if completed:
                agents_to_run = [a for a in agents_to_run if a not in completed]
                skipped_agents = completed
                self.tracker.note(
                    f"♻️ Resuming — already done: {', '.join(sorted(completed))} (skipping these)."
                )
                logger.info(f"Resuming {self.project.slug}: skipping {completed}")
            partial = [a for a in agents_to_run
                       if agent_progress.get(a, {}).get("files")]
            if partial:
                self.tracker.note(
                    f"↩️ Continuing the partial work from: {', '.join(sorted(partial))}."
                )

        # Cumulative set of agents finished across ALL runs of this build. Seed it
        # from the checkpoint's skipped agents so resumed runs don't forget prior
        # progress when they write the next checkpoint.
        completed_agents = set(skipped_agents)

        # Mark skipped agents
        for agent_name in skipped_agents:
            await self.tracker.step_skip(f"agent:{agent_name}", "Completed in previous run")

        # Agents that are optional — failure doesn't block the pipeline
        optional_agents = {"qa"}

        for agent_name in agents_to_run:
            step_key = f"agent:{agent_name}"
            await self.tracker.step_start(step_key, "Running...")

            # Persist file writes live, so even a hard crash mid-agent (OOM, bot
            # restart) leaves a resumable checkpoint of what was already produced.
            def _live_progress(files: list, session_id: str, _agent=agent_name):
                self._record_progress(agent_progress, _agent, files, session_id,
                                      status="running")
                save_checkpoint(project_dir, _agent, "partial", self.report,
                                completed_agents, agent_progress)

            agent_tokens = await self._run_agent(
                agent_name, project_dir,
                extra_context=self._resume_context(agent_name, agent_progress),
                on_progress=_live_progress,
            )
            self.report.add(agent_tokens)
            self._record_progress(agent_progress, agent_name, agent_tokens.files_written,
                                  agent_tokens.session_id,
                                  status="done" if agent_tokens.success else "failed",
                                  error=agent_tokens.error)

            if not agent_tokens.success:
                # Save the partial progress (files written so far) before retrying.
                save_checkpoint(project_dir, agent_name, "partial", self.report,
                                completed_agents, agent_progress)

                # Retry — it receives the resume context (existing files + the
                # error), so it continues rather than starting from scratch.
                await self.tracker.step_start(step_key, "Retrying with error fix...")
                self.tracker.note(
                    f"⚠️ {agent_name.title()} hit an error — retrying with the fix applied "
                    f"(keeping the work it already did)."
                )
                self.tracker.log(f"   {agent_name} error: {agent_tokens.error[:200]}")

                retry_tokens = await self._run_agent(
                    agent_name, project_dir,
                    extra_context=self._resume_context(agent_name, agent_progress),
                    on_progress=_live_progress,
                )
                self.report.add(retry_tokens)
                self._record_progress(agent_progress, agent_name, retry_tokens.files_written,
                                      retry_tokens.session_id,
                                      status="done" if retry_tokens.success else "failed",
                                      error=retry_tokens.error)

                if not retry_tokens.success:
                    if agent_name in optional_agents:
                        # Optional agent — warn but continue
                        self.tracker.note(
                            f"⚠️ {agent_name.title()} couldn't finish, but it's optional — "
                            f"continuing the build without it."
                        )
                        await self.tracker.step_done(step_key, f"⚠️ Skipped (failed but non-blocking)")
                        save_checkpoint(project_dir, agent_name, "partial", self.report,
                                        completed_agents, agent_progress)
                        continue
                    else:
                        save_checkpoint(project_dir, agent_name, "failed", self.report,
                                        completed_agents, agent_progress)
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

            # Agent succeeded — record it in the cumulative set and checkpoint.
            completed_agents.add(agent_name)
            save_checkpoint(project_dir, agent_name, "partial", self.report,
                            completed_agents, agent_progress)

        # All agents done
        save_checkpoint(project_dir, "all", "complete", self.report,
                        completed_agents, agent_progress)
        return True

    @staticmethod
    def _record_progress(agent_progress: dict, agent_name: str, files, session_id: str = "",
                         status: str = "running", error: str = ""):
        """Merge an agent's latest progress into the cumulative breadcrumb map.

        Files accumulate across attempts (they persist on disk), the most recent
        session id wins, and a finished agent is never downgraded back to
        "running" by a late callback.
        """
        prev = agent_progress.get(agent_name, {})
        merged_files = sorted(set(prev.get("files", [])) | set(files or []))
        if prev.get("status") == "done" and status == "running":
            status = "done"
        agent_progress[agent_name] = {
            "status": status,
            "files": merged_files,
            "session_id": session_id or prev.get("session_id", ""),
            "error": "" if status == "done" else (error or prev.get("error", ""))[:300],
        }

    def _resume_context(self, agent_name: str, agent_progress: dict) -> str:
        """Build the extra prompt that tells a resumed/retried agent to continue
        from its partial work instead of regenerating finished files."""
        prog = agent_progress.get(agent_name)
        if not prog or prog.get("status") == "done":
            return ""
        files = prog.get("files") or []
        err = (prog.get("error") or "").strip()
        if not files and not err:
            return ""
        parts = [
            "\n\nRESUMING A PREVIOUS ATTEMPT — DO NOT START FROM SCRATCH.",
            "An earlier run of this agent did not finish, but its work is already "
            "saved in the project directory. Read what already exists, KEEP "
            "whatever is correct, and only finish or fix what is incomplete — do "
            "not regenerate files that are already done.",
        ]
        if files:
            shown = ", ".join(files[:40])
            more = "" if len(files) <= 40 else f" (+{len(files) - 40} more)"
            parts.append(f"Files the previous attempt already created: {shown}{more}")
        if err:
            parts.append(f"The previous attempt ended with this error:\n{err}")
        return "\n".join(parts)

    async def _run_agent(self, agent_name: str, project_dir: Path,
                         extra_context: str = "", on_progress=None) -> AgentTokens:
        """Run a single specialist agent via Claude Code.

        `on_progress(files, session_id)` (optional) is called whenever the agent
        creates/edits a new file, so the caller can checkpoint partial work.
        """
        tokens = AgentTokens(agent_name=agent_name)
        start_time = time.time()
        # Defined up-front so it's always in scope for the return/except paths.
        files_written = set()

        prompt_template = AGENT_PROMPTS.get(agent_name)
        if not prompt_template:
            tokens.error = f"Unknown agent: {agent_name}"
            return tokens

        stack = self.project.stack or stacks.normalize_stack(self.project.app_type)
        prompt = prompt_template.format(
            project_dir=str(project_dir),
            port=self.project.port,
            stack=stacks.stack_display(stack),
            brief=self.project.brief,
        )
        prompt += "\n\n" + stacks.stack_rules(stack, self.project.port, self.project.db_kind)

        # Point design-sensitive agents at any reference images the user attached.
        refs_dir = project_dir / "design_refs"
        if refs_dir.exists() and agent_name in ("architect", "frontend", "integrator", "qa"):
            refs = sorted(p.name for p in refs_dir.iterdir() if p.is_file())
            if refs:
                prompt += ("\n\nDESIGN REFERENCES: ./design_refs/ contains user-provided image(s): "
                           + ", ".join(refs[:10])
                           + ". Use the Read tool to view them and match their layout, colours, and branding.")

        if extra_context:
            prompt += extra_context

        # Determine max turns and timeout based on agent type
        agent_max_turns = {
            "architect": 15,
            "backend": 30,
            "database": 20,
            "frontend": 30,
            "integrator": 25,
            "qa": 20,
        }
        max_turns = agent_max_turns.get(agent_name, 20)

        # Scale the per-agent wall-clock budget by project complexity so that a
        # `complex`/`massive` brief isn't killed mid-step. The idle hang-detector
        # (AGENT_IDLE_TIMEOUT) still catches genuinely stuck agents.
        complexity = (self.estimate.complexity or self.project.complexity or "moderate").lower()
        multiplier = COMPLEXITY_TIME_MULTIPLIER.get(complexity, 1.5)
        base_timeout = BASE_AGENT_TIMEOUTS.get(agent_name, 900)
        timeout = int(base_timeout * multiplier)

        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", str(max_turns),
            "--output-format", "stream-json",
            "--verbose",
        ]

        env_vars = dict(os.environ)
        if config.ANTHROPIC_API_KEY:
            env_vars["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY

        self.tracker.note(
            f"🤖 {agent_name.title()} agent started — up to {timeout // 60} min "
            f"({complexity} project)."
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_dir),
                env=env_vars,
                limit=STREAM_BUFFER_LIMIT,
            )

            # Stream stdout for live progress updates
            stdout_chunks = []
            last_update_time = 0
            tool_count = 0
            current_action = ""

            try:
                async def read_stream():
                    nonlocal last_update_time, tool_count, current_action
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                process.stdout.readline(), timeout=AGENT_IDLE_TIMEOUT
                            )
                        except ValueError:
                            # One stream-json line still exceeded the (64 MB) buffer.
                            # asyncio has already dropped it from the buffer, so just
                            # skip this progress line and keep reading rather than
                            # letting one giant line fail the whole agent.
                            self.tracker.log(
                                f"⚠️ Agent '{agent_name}': skipped an oversized output line"
                            )
                            continue
                        if not line:
                            break
                        stdout_chunks.append(line)

                        # Try to parse each line as JSON for streaming updates
                        try:
                            event = json.loads(line.decode("utf-8", errors="replace"))
                            event_type = event.get("type", "")

                            # Track tool use for progress + the live "current action"
                            if event_type == "assistant" and "message" in event:
                                msg = event["message"]
                                if isinstance(msg, dict):
                                    for block in msg.get("content", []):
                                        if isinstance(block, dict) and block.get("type") == "tool_use":
                                            tool_count += 1
                                            tool_name = block.get("name", "")
                                            inp = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
                                            fpath = inp.get("file_path") or inp.get("path") or ""
                                            fname = Path(fpath).name if fpath else ""
                                            if tool_name in ("Write", "Edit", "NotebookEdit") and fname:
                                                if fname not in files_written:
                                                    files_written.add(fname)
                                                    # Checkpoint partial work as each
                                                    # new file lands (crash-safe resume).
                                                    if on_progress:
                                                        try:
                                                            on_progress(sorted(files_written),
                                                                        tokens.session_id)
                                                        except Exception:
                                                            logger.exception(
                                                                "on_progress callback failed (continuing)"
                                                            )
                                                current_action = f"✍️ {fname}"
                                            elif tool_name == "Read" and fname:
                                                current_action = f"📖 {fname}"
                                            elif tool_name == "Bash":
                                                current_action = "⚙️ shell command"
                                            elif tool_name in ("Grep", "Glob"):
                                                current_action = "🔎 searching"
                                            elif tool_name:
                                                current_action = tool_name

                            # Update progress responsively (min 2s between edits to
                            # stay under Telegram's edit-rate limit).
                            now = time.time()
                            if now - last_update_time >= 2:
                                last_update_time = now
                                detail = f"{tool_count} actions"
                                if current_action:
                                    detail += f" · {current_action}"
                                elif files_written:
                                    detail += f" · {', '.join(list(files_written)[-2:])}"
                                step = self.tracker.get_step(f"agent:{agent_name}")
                                if step and step.status == "running":
                                    step.detail = detail
                                    await self.tracker._update_message()

                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass

                await asyncio.wait_for(read_stream(), timeout=timeout + 60)
                stderr_bytes = await process.stderr.read()
                await process.wait()

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                elapsed = int(time.time() - start_time)
                # Either the agent went silent for AGENT_IDLE_TIMEOUT (hung) or it
                # exceeded its complexity-scaled wall-clock budget.
                tokens.error = (
                    f"Agent '{agent_name}' timed out after {elapsed // 60} min "
                    f"(budget {timeout // 60} min, hang limit {AGENT_IDLE_TIMEOUT // 60} min)"
                )
                tokens.duration_seconds = time.time() - start_time
                tokens.files_written = sorted(files_written)
                self.tracker.log(f"⏱️ Agent '{agent_name}' timed out after {elapsed // 60}min")
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
                        tokens.session_id = data.get("session_id", "") or tokens.session_id
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
                # Capture the best available error info
                error_parts = []
                if stderr.strip():
                    error_parts.append(stderr.strip()[-500:])
                # Also check stdout for error JSON
                for line in reversed(stdout.strip().split("\n")):
                    try:
                        data = json.loads(line)
                        if data.get("type") == "result" and data.get("is_error"):
                            error_parts.append(data.get("result", "")[:500])
                            break
                        if "error" in data:
                            error_parts.append(str(data["error"])[:500])
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
                tokens.error = "\n".join(error_parts) if error_parts else f"Exit code {process.returncode}"
                tokens.success = False
            else:
                tokens.success = True

            if tokens.success:
                self.tracker.note(
                    f"✅ {agent_name.title()} done — {len(files_written)} file(s), "
                    f"{tokens.duration_seconds:.0f}s, ${tokens.cost_usd:.3f}."
                )
            # Keep the full metrics in the build log for /logs and cost analysis.
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

        # Record the files this agent touched (used to resume partial work).
        tokens.files_written = sorted(files_written)
        return tokens

    def get_report(self) -> BuildReport:
        return self.report
