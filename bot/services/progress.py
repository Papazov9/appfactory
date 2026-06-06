from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from bot.models.project import Project, ProjectStatus, db

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Plain-language explainers shown for the active step
# ──────────────────────────────────────────────
#  Keyed by step key (agent steps use "agent:<name>"). Shown as a dim line under
#  the progress bar so the user always knows what the bot is doing right now.
STEP_EXPLAINERS: dict[str, str] = {
    "estimate": "Sizing the work and estimating the cost before building.",
    "approval": "Waiting for you to approve the build.",
    "agent:architect": "Designing the file layout, data model and dependencies.",
    "agent:backend": "Writing the server, API routes and business logic.",
    "agent:database": "Creating the schema, data access and realistic seed data.",
    "agent:frontend": "Building the user interface — layout, styling and states.",
    "agent:integrator": "Wiring the frontend to the backend and fixing build issues.",
    "agent:qa": "Reviewing the code and polishing it before deploy.",
    "agent:updater": "Applying your requested changes to the existing code.",
    "agent:composer": "Reading the repo and writing a docker-compose so it can deploy.",
    "docker_build": "Building the container image your app runs inside.",
    "docker_start": "Starting the container(s) and provisioning the database.",
    "health_check": "Checking the app actually answers HTTP on its port.",
    "tunnel": "Pointing your public subdomain at the running app.",
    "verify": "Loading the live URL to confirm it responds.",
}


# ──────────────────────────────────────────────
#  Failure diagnosis — turn a raw error into a friendly, actionable explanation
# ──────────────────────────────────────────────
#  Each entry: (compiled regex, explanation+fix). First match wins. Patterns are
#  ordered most-specific first.
_FAILURE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"credit balance is too low|insufficient.*credit|billing.*credit", re.I),
     "Your Anthropic API credit is exhausted. Top up at console.anthropic.com → Billing "
     "(make sure it's the SAME organisation as your API key), then retry."),
    (re.compile(r"invalid api key|authentication_error|invalid x-api-key|fix external api key", re.I),
     "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in the bot's .env "
     "(no quotes, not truncated), then restart the bot."),
    (re.compile(r"\bclaude\b.*(not found|no such file)|command not found.*claude", re.I),
     "The `claude` CLI isn't installed or on PATH for the bot's user. Install Claude Code "
     "on the server, then retry."),
    (re.compile(r"isn't publishing|no service publishes a web port|publish.*port.*route", re.I),
     "The web service isn't publishing a port the bot can route to. Add a `ports:` mapping "
     "to your public/web service in the compose file, then retry."),
    (re.compile(r"docker compose up failed|compose.*(failed|error)", re.I),
     "The compose stack failed to build or start. The log below shows which service failed — "
     "usually a build error in one service, or a bad image/port."),
    (re.compile(r"docker build failed|failed to build|returned a non-zero code|"
                r"COPY failed|executor failed|error building", re.I),
     "The container image failed to build — usually a missing or mismatched dependency in "
     "your manifest, or a Dockerfile step that errored. The failing command is in the log below."),
    (re.compile(r"health check|not responding|didn't become healthy|connection refused|"
                r"crashed on startup|exited \(", re.I),
     "The app started but didn't answer HTTP on its port in time. Common causes: it doesn't "
     "read $PORT / bind 0.0.0.0, it crashed on boot (see logs), or a runtime dependency is missing."),
    (re.compile(r"permission denied.*cloudflared|cloudflared.*permission|writing to /etc/cloudflared", re.I),
     "The bot can't write the Cloudflare tunnel config. Give the appfactory user write access "
     "to the cloudflared config (chown or sudoers), then retry."),
    (re.compile(r"git (clone|push|fetch).*(failed|error)|could not read from remote|"
                r"repository not found|authentication failed", re.I),
     "A Git operation failed — usually auth (the GITHUB_TOKEN can't access this repo) or the "
     "repo/branch doesn't exist. Confirm the repo is under your token's account."),
    (re.compile(r"no available ports", re.I),
     "All deploy ports in the configured range are in use. Stop or delete an old project, "
     "or widen PORT_RANGE_START/END in .env."),
    (re.compile(r"postgres did not report ready|database not ready|database bootstrap", re.I),
     "The database didn't become ready in time. Retry; if it persists, the DB image or "
     "credentials may be misconfigured."),
    (re.compile(r"timed out|timeout", re.I),
     "A step ran past its time budget. Big or complex projects legitimately need longer — "
     "retry (it resumes from where it stopped). If it always stalls at the same step, that "
     "step is likely stuck."),
]


def explain_failure(error: str) -> str:
    """Return a friendly, actionable explanation for a raw error, or '' if none matches."""
    if not error:
        return ""
    for pattern, message in _FAILURE_PATTERNS:
        if pattern.search(error):
            return message
    return ""


# ──────────────────────────────────────────────
#  Pipeline step definitions
# ──────────────────────────────────────────────

class StepStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

STEP_ICONS = {
    StepStatus.PENDING: "⬜",
    StepStatus.RUNNING: "🔄",
    StepStatus.DONE: "✅",
    StepStatus.FAILED: "❌",
    StepStatus.SKIPPED: "⏭️",
}


class PipelineStep:
    """A single step in the build pipeline."""

    def __init__(self, key: str, label: str, pct_start: int, pct_end: int):
        self.key = key
        self.label = label
        self.pct_start = pct_start
        self.pct_end = pct_end
        self.status = StepStatus.PENDING
        self.detail: str = ""
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    @property
    def elapsed(self) -> str:
        if not self.started_at:
            return ""
        end = self.finished_at or time.time()
        secs = int(end - self.started_at)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m{secs % 60:02d}s"

    def start(self, detail: str = ""):
        self.status = StepStatus.RUNNING
        self.detail = detail
        self.started_at = time.time()

    def done(self, detail: str = ""):
        self.status = StepStatus.DONE
        if detail:
            self.detail = detail
        self.finished_at = time.time()

    def fail(self, detail: str = ""):
        self.status = StepStatus.FAILED
        if detail:
            self.detail = detail
        self.finished_at = time.time()

    def skip(self, detail: str = ""):
        self.status = StepStatus.SKIPPED
        if detail:
            self.detail = detail

    def format_line(self) -> str:
        icon = STEP_ICONS[self.status]
        elapsed = f" ({self.elapsed})" if self.elapsed and self.status in (StepStatus.DONE, StepStatus.RUNNING) else ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"{icon} {self.label}{detail}{elapsed}"


def build_pipeline_steps(agents_needed: list[str]) -> list[PipelineStep]:
    """Create the full pipeline step list based on which agents will run."""
    steps = []
    steps.append(PipelineStep("estimate", "📊 Cost estimation", 0, 5))
    steps.append(PipelineStep("approval", "⏸️ Awaiting approval", 5, 7))

    # Agent steps take 7% to 80% of the bar, split evenly
    agent_labels = {
        "architect": "🧠 Architect — Planning structure",
        "backend": "⚙️ Backend — Building server & API",
        "database": "🗄️ Database — Setting up data layer",
        "frontend": "🎨 Frontend — Crafting the UI",
        "integrator": "🔗 Integrator — Wiring it together",
        "qa": "🔍 QA — Testing & polishing",
        "updater": "🔧 Updater — Applying changes",
    }

    if agents_needed:
        pct_per_agent = 73 // len(agents_needed)  # 7% to 80%
        for i, agent in enumerate(agents_needed):
            pct_start = 7 + i * pct_per_agent
            pct_end = pct_start + pct_per_agent
            label = agent_labels.get(agent, f"🤖 {agent.title()}")
            steps.append(PipelineStep(f"agent:{agent}", label, pct_start, pct_end))

    steps.append(PipelineStep("docker_build", "🐳 Docker — Building image", 80, 85))
    steps.append(PipelineStep("docker_start", "🐳 Docker — Starting container", 85, 88))
    steps.append(PipelineStep("health_check", "💓 Health check", 88, 92))
    steps.append(PipelineStep("tunnel", "🌐 Tunnel — Routing subdomain", 92, 97))
    steps.append(PipelineStep("verify", "🔗 Verify — Testing live URL", 97, 100))

    return steps


# ──────────────────────────────────────────────
#  Progress Tracker
# ──────────────────────────────────────────────

class ProgressTracker:
    """Sends and updates a progress message in Telegram for a project build."""

    def __init__(self, bot: Bot, project: Project):
        self.bot = bot
        self.project = project
        self.steps: list[PipelineStep] = []
        self._pipeline_started_at: Optional[float] = None
        self._last_text: Optional[str] = None
        # Recent human-facing narration shown as a live feed in the message.
        self.activity: list[str] = []

    def init_steps(self, agents_needed: list[str]):
        """Initialize pipeline steps once we know which agents will run."""
        self.steps = build_pipeline_steps(agents_needed)
        self._pipeline_started_at = time.time()

    def get_step(self, key: str) -> Optional[PipelineStep]:
        for s in self.steps:
            if s.key == key:
                return s
        return None

    def current_pct(self) -> int:
        """Calculate current progress percentage from steps."""
        if not self.steps:
            return 0
        for step in reversed(self.steps):
            if step.status == StepStatus.RUNNING:
                return step.pct_start
            if step.status in (StepStatus.DONE, StepStatus.SKIPPED):
                return step.pct_end
        return 0

    # ── Logging ──────────────────────────────

    def log(self, message: str):
        """Append a timestamped entry to build_log."""
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        if self.project.build_log:
            self.project.build_log += f"\n{entry}"
        else:
            self.project.build_log = entry
        logger.info(f"[{self.project.slug}] {message}")

    def note(self, message: str):
        """User-facing narration: shows in the live message feed AND the build log.

        Use this (instead of log) for milestones worth surfacing in the Telegram
        UI — what the bot just did/decided. The feed refreshes on the next message
        edit (step transitions / the agent's periodic updates)."""
        self.activity.append(message)
        if len(self.activity) > 8:
            self.activity = self.activity[-8:]
        self.log(message)

    async def anote(self, message: str):
        """Like note(), but refresh the Telegram message immediately."""
        self.note(message)
        await self._update_message()

    # ── Step transitions with logging ────────

    async def step_start(self, key: str, detail: str = ""):
        step = self.get_step(key)
        if step:
            step.start(detail)
            self.log(f"▶ {step.label}" + (f": {detail}" if detail else ""))
        await self._update_message()

    async def step_done(self, key: str, detail: str = ""):
        step = self.get_step(key)
        if step:
            step.done(detail)
            self.log(f"✓ {step.label}" + (f": {detail}" if detail else f" ({step.elapsed})"))
        await self._update_message()
        await db.save(self.project)

    async def step_fail(self, key: str, detail: str = ""):
        step = self.get_step(key)
        if step:
            step.fail(detail)
            self.log(f"✗ {step.label}" + (f": {detail}" if detail else ""))
        await self._update_message()
        await db.save(self.project)

    async def step_skip(self, key: str, detail: str = ""):
        step = self.get_step(key)
        if step:
            step.skip(detail)
            self.log(f"⏭ {step.label}" + (f": {detail}" if detail else ""))
        await self._update_message()

    # ── High-level actions ───────────────────

    async def send_initial(self):
        """Send the first progress message and store the message ID."""
        text = self._format_message()
        msg = await self.bot.send_message(
            chat_id=self.project.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        self.project.telegram_progress_msg_id = msg.message_id
        await db.save(self.project)

    async def update(self, status: ProjectStatus, extra_info: str = ""):
        """Update project status and refresh the Telegram message."""
        self.project.status = status
        if extra_info:
            self.log(extra_info)
        await db.save(self.project)
        await self._update_message()

    def _whats_running(self) -> str:
        """A short, friendly description of what was deployed (for the success message)."""
        mode = getattr(self.project, "deploy_mode", "")
        if mode == "compose":
            svc = getattr(self.project, "web_service", "") or "web"
            return f"🧩 Running your docker-compose stack (subdomain → <code>{html.escape(svc)}</code>)."
        if mode == "dockerfile":
            return "🐳 Running your container from the repo's own Dockerfile."
        return "🐳 Running in its own container behind your Cloudflare subdomain."

    async def complete(self, url: str):
        """Mark the project as live and send the final URL."""
        self.project.status = ProjectStatus.LIVE
        self.project.url = url
        self.note(f"🎉 Live at {url}")
        await db.save(self.project)

        text = self._format_message()
        text += (
            f"\n\n✅ <b>Deployed successfully</b>\n"
            f"{self._whats_running()}\n"
            f"🔗 <b>Your app is live:</b>\n<a href=\"{url}\">{url}</a>"
        )

        try:
            await self.bot.edit_message_text(
                chat_id=self.project.telegram_chat_id,
                message_id=self.project.telegram_progress_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        except Exception:
            await self.bot.send_message(
                chat_id=self.project.telegram_chat_id,
                text=f"✅ <b>{html.escape(self.project.name)}</b> is live!\n\n🔗 <a href=\"{url}\">{url}</a>",
                parse_mode=ParseMode.HTML,
            )

    async def fail(self, error: str):
        """Mark the project as failed and notify the user with a friendly diagnosis."""
        self.project.status = ProjectStatus.FAILED
        self.project.error_log = error
        self.note(f"❌ Failed: {error[:140]}")
        await db.save(self.project)

        # Which retry command makes sense for this kind of project?
        mode = getattr(self.project, "deploy_mode", "")
        retry_cmd = "/redeploy" if mode in ("compose", "dockerfile") else "/rebuild"
        pid = self.project.id

        diagnosis = explain_failure(error)
        short_error = error[:700] + "..." if len(error) > 700 else error

        text = self._format_message()
        text += "\n\n❌ <b>Build failed</b>"
        if diagnosis:
            text += f"\n💡 <b>Likely cause:</b> {diagnosis}"
        text += f"\n\n<pre>{html.escape(short_error)}</pre>"
        text += (
            f"\n🔁 <code>{retry_cmd} {pid}</code> to retry"
            f" · <code>/logs {pid}</code> for the full output"
        )

        try:
            await self.bot.edit_message_text(
                chat_id=self.project.telegram_chat_id,
                message_id=self.project.telegram_progress_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await self.bot.send_message(
                chat_id=self.project.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )

    # ── Message formatting ───────────────────

    async def _update_message(self):
        """Edit the Telegram progress message (skips no-op edits)."""
        if not self.project.telegram_progress_msg_id:
            return
        text = self._format_message()
        if text == self._last_text:
            return  # nothing changed — avoid a redundant API call / "not modified"
        try:
            await self.bot.edit_message_text(
                chat_id=self.project.telegram_chat_id,
                message_id=self.project.telegram_progress_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            self._last_text = text
        except Exception as e:
            logger.debug(f"Could not edit progress message: {e}")

    def _activity_lines(self, count: int) -> list[str]:
        if not self.activity:
            return []
        out = ["", "<b>📡 Activity</b>"]
        for item in self.activity[-count:]:
            out.append(f"<i>· {html.escape(item)}</i>")
        return out

    def _format_message(self) -> str:
        from bot.config import config as _config

        lines = [
            f"🏗️ <b>#{self.project.id} {html.escape(self.project.name)}</b>",
            f"🌐 <code>{self.project.slug}.{_config.BASE_DOMAIN}</code>",
        ]

        # Progress bar
        pct = self.current_pct()
        if self.project.status == ProjectStatus.LIVE:
            pct = 100
        elif self.project.status == ProjectStatus.FAILED:
            pct = max(pct, 0)

        filled = int(pct / 5)
        empty = 20 - filled
        bar = "▓" * filled + "░" * empty
        lines.append(f"\n<code>{bar} {pct}%</code>")

        # Elapsed time
        if self._pipeline_started_at:
            elapsed = int(time.time() - self._pipeline_started_at)
            if elapsed >= 60:
                lines.append(f"⏱️ {elapsed // 60}m {elapsed % 60}s elapsed")
            else:
                lines.append(f"⏱️ {elapsed}s elapsed")

        # "What's happening now" — a plain-language explainer for the active step.
        running = next((s for s in self.steps if s.status == StepStatus.RUNNING), None)
        if running:
            explainer = STEP_EXPLAINERS.get(running.key)
            if explainer:
                lines.append(f"ℹ️ <i>{html.escape(explainer)}</i>")

        # Step checklist
        if self.steps:
            lines.append("")
            for step in self.steps:
                lines.append(step.format_line())

        # Live activity feed
        lines += self._activity_lines(5)

        # Truncate for Telegram's 4096 char limit
        text = "\n".join(lines)
        if len(text) > 3800:
            # Keep header + bar + explainer, only non-pending steps, and a short feed.
            visible = [s for s in self.steps if s.status != StepStatus.PENDING]
            head = 5 if running and STEP_EXPLAINERS.get(running.key) else 4
            lines_trimmed = lines[:head]
            lines_trimmed.append("")
            for step in visible:
                lines_trimmed.append(step.format_line())
            remaining = len(self.steps) - len(visible)
            if remaining > 0:
                lines_trimmed.append(f"   ... {remaining} more steps pending")
            lines_trimmed += self._activity_lines(3)
            text = "\n".join(lines_trimmed)

        return text
