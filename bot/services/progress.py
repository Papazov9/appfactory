from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from bot.models.project import Project, ProjectStatus, db

logger = logging.getLogger(__name__)


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

    async def complete(self, url: str):
        """Mark the project as live and send the final URL."""
        self.project.status = ProjectStatus.LIVE
        self.project.url = url
        self.log(f"🎉 Project is LIVE at {url}")
        await db.save(self.project)

        text = self._format_message()
        text += f"\n\n🔗 <b>Your app is live:</b>\n<a href=\"{url}\">{url}</a>"

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
                text=f"✅ <b>{self.project.name}</b> is live!\n\n🔗 <a href=\"{url}\">{url}</a>",
                parse_mode=ParseMode.HTML,
            )

    async def fail(self, error: str):
        """Mark the project as failed and notify the user."""
        self.project.status = ProjectStatus.FAILED
        self.project.error_log = error
        self.log(f"❌ FAILED: {error[:300]}")
        await db.save(self.project)

        text = self._format_message()
        short_error = error[:500] + "..." if len(error) > 500 else error
        text += f"\n\n<pre>{short_error}</pre>"
        text += "\n\nUse /rebuild to retry, or /logs to see full output."

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
        """Edit the Telegram progress message."""
        if not self.project.telegram_progress_msg_id:
            return
        text = self._format_message()
        try:
            await self.bot.edit_message_text(
                chat_id=self.project.telegram_chat_id,
                message_id=self.project.telegram_progress_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.debug(f"Could not edit progress message: {e}")

    def _format_message(self) -> str:
        from bot.config import config as _config

        lines = [
            f"🏗️ <b>#{self.project.id} {self.project.name}</b>",
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

        # Step checklist
        if self.steps:
            lines.append("")
            for step in self.steps:
                lines.append(step.format_line())

        # Truncate for Telegram's 4096 char limit
        text = "\n".join(lines)
        if len(text) > 3800:
            # Keep header + bar + only non-pending steps
            visible = [s for s in self.steps if s.status != StepStatus.PENDING]
            lines_trimmed = lines[:4]  # header + bar
            lines_trimmed.append("")
            for step in visible:
                lines_trimmed.append(step.format_line())
            remaining = len(self.steps) - len(visible)
            if remaining > 0:
                lines_trimmed.append(f"   ... {remaining} more steps pending")
            text = "\n".join(lines_trimmed)

        return text
