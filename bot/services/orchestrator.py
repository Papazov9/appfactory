from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil

from telegram import Bot

from bot.config import config
from bot.models.project import Project, ProjectStatus, db
from bot.services.agent_builder import MultiAgentBuilder
from bot.services.docker_manager import DockerManager
from bot.services.estimator import estimate_project, CostEstimate
from bot.services.progress import ProgressTracker
from bot.services.tunnel_manager import TunnelManager

logger = logging.getLogger(__name__)

# Store pending approvals: project_id -> CostEstimate
_pending_approvals: dict[int, CostEstimate] = {}


def slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text[:40].strip("-")


async def create_project(
    bot: Bot,
    chat_id: int,
    name: str,
    brief: str,
    app_type: str = "fullstack",
) -> Project:
    """Create a project, estimate cost, and ask for approval before building."""
    slug = slugify(name)

    existing = await db.get_by_slug(slug)
    if existing:
        slug = f"{slug}-{int(asyncio.get_event_loop().time()) % 10000}"

    port = await db.next_available_port()

    project = Project(
        name=name,
        slug=slug,
        brief=brief,
        app_type=app_type,
        port=port,
        telegram_chat_id=chat_id,
        status=ProjectStatus.ESTIMATING,
    )
    project = await db.save(project)

    # Run estimation in background, then ask for approval
    asyncio.create_task(_estimate_and_ask(bot, project))

    return project


async def _estimate_and_ask(bot: Bot, project: Project):
    """Estimate cost and send approval request to user."""
    tracker = ProgressTracker(bot, project)
    await tracker.send_initial()

    # Initialize with minimal steps for estimation phase
    tracker.init_steps([])
    await tracker.step_start("estimate", "Analyzing your brief...")

    try:
        estimate = await estimate_project(project.brief, project.app_type)

        # Now we know the agents — reinitialize steps with full pipeline
        tracker.init_steps(estimate.agents_needed)
        await tracker.step_done("estimate", f"{estimate.tier_label} — {estimate.complexity}")
        await tracker.step_start("approval")

        # Store the estimate
        project.estimated_cost_usd = estimate.estimated_cost_usd
        project.complexity = estimate.complexity
        project.status = ProjectStatus.AWAITING_APPROVAL
        await db.save(project)

        # Store for approval lookup
        _pending_approvals[project.id] = estimate

        # Send detailed estimate to user
        estimate_text = estimate.format_telegram()
        await bot.send_message(
            chat_id=project.telegram_chat_id,
            text=(
                f"🏭 <b>Project #{project.id}: {project.name}</b>\n\n"
                f"{estimate_text}\n\n"
                f"Send <code>/approve {project.id}</code> to start building\n"
                f"Send <code>/cancel_build {project.id}</code> to cancel"
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception(f"Estimation failed for {project.slug}")
        tracker.log(f"⚠️ Estimation failed: {e}")
        # Fall back to auto-approve with default estimate
        from bot.services.estimator import _heuristic_estimate
        estimate = _heuristic_estimate(project.brief, project.app_type)
        tracker.init_steps(estimate.agents_needed)
        await tracker.step_done("estimate", "Heuristic fallback (API failed)")
        await tracker.step_done("approval", "Auto-approved (estimation failed)")
        _pending_approvals[project.id] = estimate
        await _run_pipeline(bot, project, estimate, tracker)


async def approve_project(bot: Bot, project_id: int) -> str:
    """Approve a project for building. Returns status message."""
    project = await db.get(project_id)
    if not project:
        return f"Project #{project_id} not found."

    if project.status != ProjectStatus.AWAITING_APPROVAL:
        return f"Project #{project_id} is not awaiting approval (status: {project.status.value})."

    estimate = _pending_approvals.pop(project_id, None)
    if not estimate:
        from bot.services.estimator import _heuristic_estimate
        estimate = _heuristic_estimate(project.brief, project.app_type)

    # Create tracker with full pipeline steps
    tracker = ProgressTracker(bot, project)
    tracker.init_steps(estimate.agents_needed)

    # Mark estimate and approval as done
    step_est = tracker.get_step("estimate")
    if step_est:
        step_est.done(f"{estimate.tier_label}")
    step_appr = tracker.get_step("approval")
    if step_appr:
        step_appr.done("Approved")

    # Start the build
    asyncio.create_task(_run_pipeline(bot, project, estimate, tracker))
    return f"✅ Building #{project_id} — {project.name}!"


async def cancel_pending(bot: Bot, project_id: int) -> str:
    """Cancel a pending project."""
    project = await db.get(project_id)
    if not project:
        return f"Project #{project_id} not found."

    _pending_approvals.pop(project_id, None)
    project.status = ProjectStatus.STOPPED
    await db.save(project)
    return f"Cancelled #{project_id}."


async def _run_pipeline(bot: Bot, project: Project, estimate: CostEstimate,
                        tracker: ProgressTracker):
    """Full pipeline: multi-agent build → dockerize → tunnel → verify → live."""
    try:
        # ── Phase 1: Multi-agent build ──────────────
        project.status = ProjectStatus.BUILDING
        await db.save(project)

        builder = MultiAgentBuilder(project, tracker, estimate)
        success = await builder.build()

        # Save the build report
        report = builder.get_report()
        project.actual_cost_usd = report.total_cost_usd
        project.total_input_tokens = report.total_input_tokens
        project.total_output_tokens = report.total_output_tokens
        project.build_report_json = json.dumps({
            "agents": [
                {
                    "name": a.agent_name,
                    "input_tokens": a.input_tokens,
                    "output_tokens": a.output_tokens,
                    "cost_usd": a.cost_usd,
                    "duration_seconds": a.duration_seconds,
                    "success": a.success,
                }
                for a in report.agents
            ],
            "total_cost_usd": report.total_cost_usd,
            "total_tokens": report.total_input_tokens + report.total_output_tokens,
        })
        await db.save(project)

        if not success:
            await bot.send_message(
                chat_id=project.telegram_chat_id,
                text=report.format_telegram(),
                parse_mode="HTML",
            )
            return

        # ── Phase 2: Docker build & start ───────────
        docker_mgr = DockerManager(project, tracker)
        success = await docker_mgr.containerize_and_run()
        if not success:
            await bot.send_message(
                chat_id=project.telegram_chat_id,
                text=report.format_telegram(),
                parse_mode="HTML",
            )
            return

        # ── Phase 3: Cloudflare Tunnel route ────────
        tunnel_mgr = TunnelManager(project, tracker)
        url = await tunnel_mgr.setup_route()
        if not url:
            await bot.send_message(
                chat_id=project.telegram_chat_id,
                text=report.format_telegram(),
                parse_mode="HTML",
            )
            return

        # ── Phase 4: Verify live URL ────────────────
        await tracker.step_start("verify", f"Testing {url}...")
        live = await _verify_live_url(url)
        if live:
            await tracker.step_done("verify", "URL responds OK")
        else:
            tracker.log(f"⚠️ URL {url} not responding yet — may need DNS propagation")
            await tracker.step_done("verify", "URL not responding yet (DNS may need time)")

        # ── Done! ───────────────────────────────────
        await tracker.complete(url)
        logger.info(f"Project {project.slug} is live at {url}")

        await bot.send_message(
            chat_id=project.telegram_chat_id,
            text=report.format_telegram(),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception(f"Pipeline failed for {project.slug}")
        await tracker.fail(f"Unexpected error: {str(e)}")


async def _verify_live_url(url: str, retries: int = 3) -> bool:
    """Check if the deployed URL is actually reachable."""
    import httpx
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code < 500:
                    return True
        except Exception:
            pass
        if attempt < retries - 1:
            await asyncio.sleep(3)
    return False


async def stop_project(bot: Bot, project: Project):
    """Stop a running project."""
    tracker = ProgressTracker(bot, project)
    docker_mgr = DockerManager(project, tracker)
    tunnel_mgr = TunnelManager(project, tracker)

    await docker_mgr.stop()
    await tunnel_mgr.remove_route()

    project.status = ProjectStatus.STOPPED
    await db.save(project)


async def delete_project(bot: Bot, project: Project):
    """Stop and fully remove a project."""
    await stop_project(bot, project)

    if project.project_dir.exists():
        shutil.rmtree(project.project_dir, ignore_errors=True)

    image_tag = f"appfactory-{project.slug}:latest"
    proc = await asyncio.create_subprocess_exec(
        "docker", "rmi", "-f", image_tag,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    await db.delete(project.id)


async def rebuild_project(bot: Bot, project: Project):
    """Rebuild — resumes from checkpoint if available."""
    tracker = ProgressTracker(bot, project)
    docker_mgr = DockerManager(project, tracker)
    await docker_mgr.stop()

    # Don't wipe project dir — checkpoints are there for resume!
    project.status = ProjectStatus.PENDING
    project.error_log = ""
    project.build_log = ""
    project.container_id = ""
    await db.save(project)

    from bot.services.estimator import _heuristic_estimate
    estimate = _heuristic_estimate(project.brief, project.app_type)

    tracker.init_steps(estimate.agents_needed)
    # Mark estimate/approval as already done for rebuilds
    step_est = tracker.get_step("estimate")
    if step_est:
        step_est.done("Rebuild")
    step_appr = tracker.get_step("approval")
    if step_appr:
        step_appr.done("Rebuild")

    await tracker.send_initial()
    asyncio.create_task(_run_pipeline(bot, project, estimate, tracker))


async def scan_projects(bot: Bot, chat_id: int) -> str:
    """Scan the projects directory and Docker containers to discover/sync project state."""
    lines = ["🔍 <b>Project Scan Results</b>\n"]
    projects_dir = config.PROJECTS_DIR

    if not projects_dir.exists():
        return "No projects directory found."

    # Get all known projects from DB
    db_projects = await db.list_all()
    db_slugs = {p.slug: p for p in db_projects}

    # Get running Docker containers
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    running_containers: dict[str, str] = {}
    for line in stdout.decode().strip().split("\n"):
        if line and line.startswith("appfactory-"):
            parts = line.split("\t")
            name = parts[0].replace("appfactory-", "")
            status = parts[1] if len(parts) > 1 else "unknown"
            running_containers[name] = status

    # Scan project directories
    discovered = 0
    updated = 0
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        slug = entry.name
        has_container = slug in running_containers
        container_status = running_containers.get(slug, "not running")

        # Check if it has app files
        has_files = any(
            (entry / f).exists()
            for f in ["package.json", "index.html", "app.py", "requirements.txt", "server.js"]
        )

        if slug in db_slugs:
            # Known project — check if state needs updating
            project = db_slugs[slug]
            icon = "🟢" if has_container else "⚪"
            state_changed = False

            if has_container and project.status not in (ProjectStatus.LIVE, ProjectStatus.BUILDING):
                project.status = ProjectStatus.LIVE
                project.url = f"https://{slug}.{config.BASE_DOMAIN}"
                state_changed = True
                updated += 1
            elif not has_container and project.status == ProjectStatus.LIVE:
                project.status = ProjectStatus.STOPPED
                state_changed = True
                updated += 1

            if state_changed:
                await db.save(project)

            status_str = f"{project.status.value}"
            if state_changed:
                status_str += " (updated)"

            lines.append(
                f"{icon} <b>#{project.id} {project.name}</b> — {status_str}\n"
                f"   Container: {container_status}"
            )
        else:
            # Unknown project directory — not in DB
            if has_files:
                icon = "🟡" if has_container else "⬜"
                lines.append(
                    f"{icon} <b>{slug}</b> — not in DB (found on disk)\n"
                    f"   Container: {container_status} | Has app files: {has_files}"
                )
                discovered += 1

    # Show containers not matching any project dir
    for cname, cstatus in running_containers.items():
        if cname not in db_slugs and not (projects_dir / cname).exists():
            lines.append(f"🟣 <b>{cname}</b> — orphan container\n   Status: {cstatus}")

    lines.append(f"\n📊 {len(db_projects)} in DB | {len(running_containers)} containers running")
    if discovered:
        lines.append(f"🆕 {discovered} project dirs not in DB")
    if updated:
        lines.append(f"🔄 {updated} projects updated")

    return "\n".join(lines)
