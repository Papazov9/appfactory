from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from bot.config import config
from bot.models.project import db, ProjectStatus
from bot.services.orchestrator import (
    stop_project, delete_project, rebuild_project,
    approve_project, cancel_pending, scan_projects,
)
from bot.services.docker_manager import DockerManager
from bot.services.progress import ProgressTracker

logger = logging.getLogger(__name__)


def auth_check(func):
    """Decorator: only allow configured user IDs."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if config.ALLOWED_USER_IDS and user_id not in config.ALLOWED_USER_IDS:
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, context)
    return wrapper


@auth_check
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "🏭 <b>AppFactory Bot</b>\n\n"
        "I turn project briefs into live web apps using AI agents.\n\n"
        "<b>Create:</b>\n"
        "/new — New project (text brief)\n"
        "/voice — New project (voice message)\n\n"
        "<b>Manage:</b>\n"
        "/approve &lt;id&gt; — Approve build after cost estimate\n"
        "/cancel_build &lt;id&gt; — Cancel a pending build\n"
        "/list — List all projects\n"
        "/status — Current build status\n"
        "/scan — Discover &amp; sync project states\n"
        "/cost &lt;id&gt; — View cost report for a project\n"
        "/logs &lt;id&gt; — View project logs\n"
        "/rebuild &lt;id&gt; — Rebuild (resumes from checkpoint)\n"
        "/stop &lt;id&gt; — Stop a project\n"
        "/delete &lt;id&gt; — Delete a project\n",
        parse_mode="HTML",
    )


@auth_check
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list — show all projects."""
    projects = await db.list_all()
    if not projects:
        await update.message.reply_text("No projects yet. Use /new to create one.")
        return

    lines = ["📋 <b>All Projects</b>\n"]
    for p in projects:
        status_icon = {
            ProjectStatus.LIVE: "🟢",
            ProjectStatus.BUILDING: "🔵",
            ProjectStatus.DOCKERIZING: "🔵",
            ProjectStatus.DEPLOYING: "🔵",
            ProjectStatus.ESTIMATING: "🟡",
            ProjectStatus.AWAITING_APPROVAL: "🟡",
            ProjectStatus.FAILED: "🔴",
            ProjectStatus.STOPPED: "⚪",
        }.get(p.status, "🟡")

        created = datetime.fromtimestamp(p.created_at).strftime("%b %d %H:%M")
        url_part = f" → <a href=\"{p.url}\">{p.url}</a>" if p.url else ""
        cost_part = f" | ${p.actual_cost_usd:.2f}" if p.actual_cost_usd > 0 else ""
        lines.append(
            f"{status_icon} <b>#{p.id}</b> {p.name} ({p.status.value}){url_part}\n"
            f"   <code>{p.slug}.{config.BASE_DOMAIN}</code> | {created}{cost_part}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@auth_check
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show currently building projects."""
    projects = await db.list_all()
    active = [
        p for p in projects
        if p.status not in (ProjectStatus.LIVE, ProjectStatus.FAILED, ProjectStatus.STOPPED)
    ]

    if not active:
        await update.message.reply_text("No active builds. Use /new to start one.")
        return

    for p in active:
        bar = p.progress_bar()
        await update.message.reply_text(
            f"🏗️ <b>#{p.id} {p.name}</b>\n<code>{bar}</code>",
            parse_mode="HTML",
        )


@auth_check
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop <id> — stop a running project."""
    project = await _get_project_from_args(update, context)
    if not project:
        return

    await update.message.reply_text(f"Stopping #{project.id} {project.name}...")
    await stop_project(context.bot, project)
    await update.message.reply_text(f"⏹️ #{project.id} stopped.")


@auth_check
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /delete <id> — fully remove a project."""
    project = await _get_project_from_args(update, context)
    if not project:
        return

    await update.message.reply_text(
        f"🗑️ Deleting #{project.id} {project.name}..."
    )
    await delete_project(context.bot, project)
    await update.message.reply_text(f"Deleted #{project.id}.")


@auth_check
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logs <id> — show build log + container logs combined."""
    project = await _get_project_from_args(update, context)
    if not project:
        return

    sections = []

    # Section 1: Build log (pipeline events)
    if project.build_log:
        build_lines = project.build_log.strip().split("\n")
        # Show last 30 lines of build log
        if len(build_lines) > 30:
            build_lines = build_lines[-30:]
        sections.append(
            "<b>📋 Build Log</b> (last events):\n"
            f"<pre>{chr(10).join(build_lines)}</pre>"
        )

    # Section 2: Error log (if any)
    if project.error_log:
        error_text = project.error_log[-500:]
        sections.append(
            f"<b>❌ Last Error:</b>\n<pre>{error_text}</pre>"
        )

    # Section 3: Container logs (if container exists)
    if project.container_id or project.status == ProjectStatus.LIVE:
        tracker = ProgressTracker(context.bot, project)
        docker_mgr = DockerManager(project, tracker)
        container_logs = await docker_mgr.get_logs(tail=30)
        if container_logs and container_logs != "No container logs available.":
            sections.append(
                f"<b>🐳 Container Logs</b> (last 30 lines):\n<pre>{container_logs[-1500:]}</pre>"
            )

    if not sections:
        sections.append("No logs available for this project.")

    # Build the final message
    header = f"📜 <b>Logs — #{project.id} {project.name}</b> ({project.status.value})\n\n"
    text = header + "\n\n".join(sections)

    # Telegram message limit is 4096 chars
    if len(text) > 4000:
        text = text[:3990] + "...\n(truncated)"

    await update.message.reply_text(text, parse_mode="HTML")


@auth_check
async def cmd_rebuild(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rebuild <id> — rebuild from stored brief."""
    project = await _get_project_from_args(update, context)
    if not project:
        return

    await update.message.reply_text(
        f"🔄 Rebuilding #{project.id} {project.name}...\n"
        f"Will resume from checkpoint if available."
    )
    await rebuild_project(context.bot, project)


@auth_check
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan — discover projects on disk and sync state."""
    await update.message.reply_text("🔍 Scanning projects directory and Docker containers...")
    result = await scan_projects(context.bot, update.effective_chat.id)
    await update.message.reply_text(result, parse_mode="HTML")


async def _get_project_from_args(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Extract project ID from command arguments."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a project ID. Example: /stop 3"
        )
        return None

    try:
        project_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid project ID. Use a number.")
        return None

    project = await db.get(project_id)
    if not project:
        await update.message.reply_text(f"Project #{project_id} not found.")
        return None

    return project


@auth_check
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /approve <id> — approve a project for building."""
    if not context.args:
        await update.message.reply_text("Usage: /approve <project_id>")
        return
    try:
        project_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid project ID.")
        return

    result = await approve_project(context.bot, project_id)
    await update.message.reply_text(result, parse_mode="HTML")


@auth_check
async def cmd_cancel_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel_build <id> — cancel a pending project."""
    if not context.args:
        await update.message.reply_text("Usage: /cancel_build <project_id>")
        return
    try:
        project_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid project ID.")
        return

    result = await cancel_pending(context.bot, project_id)
    await update.message.reply_text(result)


@auth_check
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cost <id> — show cost report for a project."""
    project = await _get_project_from_args(update, context)
    if not project:
        return

    import json as _json

    lines = [f"💰 <b>Cost Report — #{project.id} {project.name}</b>\n"]
    lines.append(f"📊 Complexity: {project.complexity or 'unknown'}")
    lines.append(f"💵 Estimated: ${project.estimated_cost_usd:.2f}")
    lines.append(f"💰 Actual: ${project.actual_cost_usd:.3f}")

    total_tokens = project.total_input_tokens + project.total_output_tokens
    lines.append(f"🪙 Tokens: {total_tokens:,} ({project.total_input_tokens:,} in / {project.total_output_tokens:,} out)")

    if project.build_report_json:
        try:
            report = _json.loads(project.build_report_json)
            agents = report.get("agents", [])
            if agents:
                lines.append(f"\n<b>Agent Breakdown:</b>")
                for a in agents:
                    status = "✅" if a["success"] else "❌"
                    agent_tokens = a["input_tokens"] + a["output_tokens"]
                    lines.append(
                        f"{status} {a['name'].title()}: "
                        f"{agent_tokens:,} tokens | "
                        f"${a['cost_usd']:.3f} | "
                        f"{a['duration_seconds']:.0f}s"
                    )
        except Exception:
            pass

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
