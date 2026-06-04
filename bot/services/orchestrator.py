from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from telegram import Bot

from bot.config import config
from bot.models.project import Project, ProjectStatus, db
from bot.services.agent_builder import MultiAgentBuilder
from bot.services.docker_manager import DockerManager
from bot.services.estimator import estimate_project, CostEstimate
from bot.services.progress import ProgressTracker
from bot.services.tunnel_manager import TunnelManager
from bot.services.git_manager import GitManager
from bot.services import stacks
from bot.services import cost_calibration

logger = logging.getLogger(__name__)

# Store pending approvals: project_id -> CostEstimate
_pending_approvals: dict[int, CostEstimate] = {}


def slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text[:40].strip("-")


def _save_design_refs(project: Project, images: list[str] | None) -> int:
    """Copy user-provided reference images into the project's design_refs/ folder.
    Returns the number of images saved."""
    if not images:
        return 0
    refs_dir = project.project_dir / "design_refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for src in images:
        try:
            shutil.copy(src, refs_dir / Path(src).name)
            saved += 1
        except Exception as e:
            logger.warning(f"Could not copy design ref {src}: {e}")
    return saved


async def create_project(
    bot: Bot,
    chat_id: int,
    name: str,
    brief: str,
    stack: str = stacks.DEFAULT_STACK,
    images: list[str] | None = None,
) -> Project:
    """Create a project, estimate cost, and ask for approval before building."""
    slug = slugify(name)

    existing = await db.get_by_slug(slug)
    if existing:
        slug = f"{slug}-{int(asyncio.get_event_loop().time()) % 10000}"

    port = await db.next_available_port()

    stack = stacks.normalize_stack(stack)
    project = Project(
        name=name,
        slug=slug,
        brief=brief,
        stack=stack,
        db_kind=stacks.default_db_for(stack),
        # app_type retained for backward-compat; mirrors the chosen stack.
        app_type=stack,
        port=port,
        telegram_chat_id=chat_id,
        status=ProjectStatus.ESTIMATING,
    )
    project = await db.save(project)

    saved = _save_design_refs(project, images)
    if saved:
        logger.info(f"Saved {saved} design reference image(s) for {project.slug}")

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
        estimate = await estimate_project(project.brief, project.stack)

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
        estimate = _heuristic_estimate(project.brief, project.stack)
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
        estimate = _heuristic_estimate(project.brief, project.stack)

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
        if report.last_session_id:
            project.claude_session_id = report.last_session_id
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

        # Record actual token usage so future estimates for this
        # (stack, complexity) get sharper.
        cost_calibration.record(
            project.stack, estimate.complexity,
            report.total_input_tokens, report.total_output_tokens,
        )

        # ── Phase 2–4: containerize, route, verify (zero-downtime aware) ──
        deployed = await deploy_project(bot, project, tracker)
        if deployed:
            logger.info(f"Project {project.slug} is live at {project.url}")
            # If this project is under Git (e.g. a major update or rebuild), keep
            # the repo in sync with what was just built and deployed.
            if project.repo_full_name:
                gm = GitManager(project, tracker)
                if await gm.commit_and_push("AppFactory build update"):
                    tracker.log("⬆️ Pushed changes to GitHub.")

        await bot.send_message(
            chat_id=project.telegram_chat_id,
            text=report.format_telegram(),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception(f"Pipeline failed for {project.slug}")
        await tracker.fail(f"Unexpected error: {str(e)}")


async def deploy_project(bot: Bot, project: Project, tracker: ProgressTracker) -> bool:
    """Containerize, route, and verify. Zero-downtime swap when a version is already live.

    First deploy / nothing running → build & start in place, then route.
    A version already live → build a new image, start it in parallel on a second
    port, health-check it, flip the tunnel route, and only then retire the old
    container. If the new version fails to build or pass health checks, the live
    version is left untouched (rollback = do nothing).
    """
    docker_mgr = DockerManager(project, tracker)
    tunnel_mgr = TunnelManager(project, tracker)

    live_running = await docker_mgr.is_running(docker_mgr.container_name)

    # ── First deploy (or nothing currently live): build & run in place ──
    if not live_running:
        if not await docker_mgr.containerize_and_run():
            return False
        url = await tunnel_mgr.setup_route()
        if not url:
            return False
        await _verify_and_complete(project, tracker, url)
        return True

    # ── Zero-downtime swap ──
    if not await docker_mgr.build_image():
        return False  # live version untouched
    if not await docker_mgr.ensure_runtime():
        await tracker.fail("Database not ready for redeploy — kept the live version.")
        return False

    old_port = project.port
    new_port = await db.next_available_port()
    staging = f"{docker_mgr.container_name}-new"

    await tracker.step_start("docker_start", f"Starting new version on port {new_port}...")
    await docker_mgr.remove_container(staging)  # clear any stale staging container
    ok, out = await docker_mgr.run_app(staging, new_port)
    if not ok:
        await docker_mgr.remove_container(staging)
        await tracker.step_fail("docker_start", "New version failed to start")
        await tracker.fail(f"Redeploy failed to start — kept the live version.\n{out[-400:]}")
        return False

    if not await docker_mgr.await_health(new_port):
        crashed = not await docker_mgr.is_running(staging)
        logs = await docker_mgr.get_logs(tail=40, name=staging) if crashed else ""
        await docker_mgr.remove_container(staging)
        await tracker.step_fail("health_check", "New version unhealthy — rolled back")
        await tracker.fail(
            "New version failed its health check — kept the previous live version."
            + (f"\n\nLogs:\n{logs[-500:]}" if logs else "")
        )
        return False

    # New version is healthy — flip the tunnel route to it.
    project.port = new_port
    await db.save(project)
    url = await tunnel_mgr.setup_route()
    if not url:
        # Routing failed — roll back the port and discard the new container.
        project.port = old_port
        await db.save(project)
        await docker_mgr.remove_container(staging)
        return False

    # Retire the old container, promote the new one to the canonical name.
    await docker_mgr.remove_container(docker_mgr.container_name)
    await docker_mgr.rename_container(staging, docker_mgr.container_name)
    project.container_id = await docker_mgr.container_id(docker_mgr.container_name)
    project.deploy_port_b = old_port
    await db.save(project)
    tracker.log(f"Swapped live traffic to the new version (port {new_port}).")

    await _verify_and_complete(project, tracker, url)
    return True


async def _verify_and_complete(project: Project, tracker: ProgressTracker, url: str):
    """Probe the live URL, then mark the project complete."""
    await tracker.step_start("verify", f"Testing {url}...")
    live = await _verify_live_url(url)
    if live:
        await tracker.step_done("verify", "URL responds OK")
    else:
        tracker.log(f"⚠️ URL {url} not responding yet — may need DNS propagation")
        await tracker.step_done("verify", "URL not responding yet (DNS may need time)")
    await tracker.complete(url)


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
    """Stop and fully remove a project: containers, db, network, volumes, image, files."""
    tracker = ProgressTracker(bot, project)
    docker_mgr = DockerManager(project, tracker)
    tunnel_mgr = TunnelManager(project, tracker)

    await tunnel_mgr.remove_route()
    await docker_mgr.teardown()

    if project.project_dir.exists():
        shutil.rmtree(project.project_dir, ignore_errors=True)

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


async def create_repo(bot: Bot, project: Project) -> str:
    """Create a private GitHub repo for the project and push its source. Returns a message."""
    if not config.github_enabled:
        return (
            "❌ GitHub is not configured.\n"
            "Add <code>GITHUB_TOKEN</code> and <code>GITHUB_OWNER</code> to the bot's .env, "
            "then restart the bot."
        )
    if not project.project_dir.exists():
        return f"❌ Project #{project.id} has no files on disk yet — build it first."

    tracker = ProgressTracker(bot, project)
    gm = GitManager(project, tracker)
    try:
        url = await gm.create_repo()
    except Exception as e:
        logger.exception(f"Repo creation failed for {project.slug}")
        return f"❌ Repo creation failed: {e}"

    return (
        f"✅ <b>GitHub repo created</b> for #{project.id} {project.name}\n\n"
        f"🔗 {url}\n\n"
        f"Clone it:\n<code>git clone {url}.git</code>\n\n"
        f"Make your changes, push to <code>{project.default_branch}</code>, then run "
        f"<code>/redeploy {project.id}</code> to deploy them."
    )


async def redeploy_project(bot: Bot, project: Project):
    """Pull the latest pushed code from GitHub and redeploy it (zero-downtime, no AI)."""
    tracker = ProgressTracker(bot, project)
    tracker.init_steps([])  # no agents — just pull + deploy
    step_est = tracker.get_step("estimate")
    if step_est:
        step_est.done("Redeploy from Git")
    step_appr = tracker.get_step("approval")
    if step_appr:
        step_appr.done("From Git")
    await tracker.send_initial()
    asyncio.create_task(_run_redeploy(bot, project, tracker))


async def _run_redeploy(bot: Bot, project: Project, tracker: ProgressTracker):
    gm = GitManager(project, tracker)
    try:
        sha = await gm.pull_latest()
        tracker.log(f"Pulled latest from Git: {sha}")
    except Exception as e:
        logger.exception(f"Redeploy git pull failed for {project.slug}")
        await tracker.fail(f"Git pull failed: {e}")
        return

    project.status = ProjectStatus.BUILDING
    await db.save(project)
    try:
        deployed = await deploy_project(bot, project, tracker)
        if deployed:
            await bot.send_message(
                chat_id=project.telegram_chat_id,
                text=(
                    f"♻️ <b>#{project.id} {project.name}</b> redeployed from Git "
                    f"(<code>{sha}</code>)."
                ),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.exception(f"Redeploy failed for {project.slug}")
        await tracker.fail(f"Redeploy error: {e}")


async def update_project(
    bot: Bot,
    chat_id: int,
    project_id: int,
    instructions: str,
    images: list[str] | None = None,
):
    """Update an existing project with new instructions, then redeploy.

    Small updates (<500 words) use a single updater agent.
    Large updates (>=500 words) go through the full multi-agent pipeline
    with the update instructions merged into the brief.
    """
    project = await db.get(project_id)
    if not project:
        await bot.send_message(chat_id=chat_id, text=f"Project #{project_id} not found.")
        return

    tracker = ProgressTracker(bot, project)
    _save_design_refs(project, images)

    # Leave the current version running — the zero-downtime swap in
    # deploy_project replaces it only once the new version is healthy.
    # Append update instructions to the brief for history.
    project.brief += f"\n\n--- UPDATE ---\n{instructions}"
    project.status = ProjectStatus.BUILDING
    project.error_log = ""
    project.build_log = ""
    await db.save(project)

    # Decide: small tweak vs major rewrite
    word_count = len(instructions.split())
    is_major = word_count >= 500

    if is_major:
        # Major update — use full multi-agent pipeline with merged brief
        tracker.log(f"📏 Large update detected ({word_count} words) — using full agent pipeline")
        from bot.services.estimator import _heuristic_estimate
        estimate = _heuristic_estimate(project.brief, project.stack)

        tracker.init_steps(estimate.agents_needed)
        step_est = tracker.get_step("estimate")
        if step_est:
            step_est.done(f"Major update ({word_count} words)")
        step_appr = tracker.get_step("approval")
        if step_appr:
            step_appr.done("Auto-approved (update)")

        await tracker.send_initial()
        asyncio.create_task(_run_pipeline(bot, project, estimate, tracker))
    else:
        # Small update — single updater agent
        tracker.log(f"📏 Small update ({word_count} words) — using single updater agent")
        update_agents = ["updater"]

        tracker.init_steps(update_agents)
        step_est = tracker.get_step("estimate")
        if step_est:
            step_est.done("Update")
        step_appr = tracker.get_step("approval")
        if step_appr:
            step_appr.done("Update")

        await tracker.send_initial()
        asyncio.create_task(_run_update_pipeline(bot, project, instructions, tracker))


async def _cached_context(project_dir) -> str:
    """A compact snapshot of the project (PLAN.md + file map) so the updater can
    locate code without re-scanning the whole tree — the cached context that
    keeps upgrades cheap."""
    parts = []
    plan = project_dir / "PLAN.md"
    if plan.exists():
        try:
            parts.append("PLAN.md (architecture from the original build):\n" + plan.read_text()[:2500])
        except Exception:
            pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", ".", "-type", "f",
            "-not", "-path", "*/node_modules/*", "-not", "-path", "*/.git/*",
            "-not", "-path", "*/target/*", "-not", "-path", "*/dist/*",
            "-not", "-path", "*/data/*",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(project_dir),
        )
        out, _ = await proc.communicate()
        files = sorted(l for l in out.decode("utf-8", errors="replace").splitlines() if l.strip())[:150]
        if files:
            parts.append("Existing files:\n" + "\n".join(files))
    except Exception:
        pass
    if not parts:
        return ""
    return "CACHED PROJECT CONTEXT (use this instead of re-scanning everything):\n\n" + "\n\n".join(parts)


async def _run_update_pipeline(bot: Bot, project: Project, instructions: str,
                                tracker: ProgressTracker):
    """Run a single updater agent on existing code, then redeploy."""
    from bot.services.agent_builder import AgentTokens, BuildReport
    import time
    import os

    report = BuildReport()

    try:
        project_dir = project.project_dir
        if not project_dir.exists():
            await tracker.fail(f"Project directory not found: {project_dir}")
            return

        # ── Run updater agent ────────────────────
        await tracker.step_start("agent:updater", "Applying changes...")

        tokens = AgentTokens(agent_name="updater")
        start_time = time.time()

        stack = project.stack or stacks.normalize_stack(project.app_type)
        cached_context = await _cached_context(project_dir)
        prompt = f"""You are updating an EXISTING project that you previously built. The code is already written and was working.

WORKING DIRECTORY: {project_dir}
PORT: {project.port}
STACK: {stacks.stack_display(stack)}

{cached_context}

The user wants these changes:
{instructions}

RULES:
1. Use the cached context above to find the right files — read ONLY what you need to change, don't re-scan the whole project
2. Make ONLY the changes requested — don't rewrite everything
3. Keep all existing functionality working
4. If adding new features, integrate them with the existing code idiomatically
5. Add any new dependencies to the project's dependency manifest
6. Make sure the app still builds and starts correctly on PORT (from env)

ORIGINAL BRIEF:
{project.brief}

""" + stacks.stack_rules(stack, project.port, project.db_kind)

        refs_dir = project_dir / "design_refs"
        if refs_dir.exists():
            refs = sorted(p.name for p in refs_dir.iterdir() if p.is_file())
            if refs:
                prompt += ("\n\nDESIGN REFERENCES: ./design_refs/ contains user-provided image(s): "
                           + ", ".join(refs[:10])
                           + ". Use the Read tool to view them and match their layout, colours, and branding.")

        # Scale max-turns and timeout based on instruction size
        word_count = len(instructions.split())
        max_turns = min(50, max(25, word_count // 10))
        timeout = min(1800, max(600, word_count * 3))  # 10min–30min

        env_vars = dict(os.environ)
        if config.ANTHROPIC_API_KEY:
            env_vars["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY

        async def _invoke(use_resume: bool):
            """Run the updater agent; resume the cached session when available."""
            resume_args = (["--resume", project.claude_session_id]
                           if use_resume and project.claude_session_id else [])
            cmd = ["claude", *resume_args, "-p", prompt,
                   "--dangerously-skip-permissions", "--max-turns", str(max_turns),
                   "--output-format", "stream-json", "--verbose"]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(project_dir), env=env_vars,
            )
            try:
                so, se = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                return (proc.returncode,
                        so.decode("utf-8", errors="replace"),
                        se.decode("utf-8", errors="replace"), False)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return None, "", "", True

        use_resume = bool(project.claude_session_id)
        tracker.log(
            f"🔧 Updater agent starting (max {max_turns} turns, {timeout // 60}min timeout)"
            + (", resuming cached context" if use_resume else "") + "..."
        )
        returncode, stdout, stderr, timed_out = await _invoke(use_resume=use_resume)
        # If resuming the old session failed, fall back to a fresh run with the
        # cached-context block (still cheaper than a full rebuild).
        if not timed_out and returncode != 0 and use_resume:
            tracker.log("⚠️ Could not resume the cached session — retrying with fresh context...")
            returncode, stdout, stderr, timed_out = await _invoke(use_resume=False)

        if timed_out:
            await tracker.step_fail("agent:updater", f"Timed out after {timeout // 60} minutes")
            await tracker.fail(f"Update agent timed out after {timeout // 60} minutes.\nTip: very large changes should use /rebuild instead.")
            return

        tokens.duration_seconds = time.time() - start_time

        # Parse tokens from stream-json output
        for line in reversed(stdout.strip().split("\n")):
            try:
                data = json.loads(line)
                if data.get("type") == "result":
                    usage = data.get("usage", {})
                    tokens.input_tokens = usage.get("input_tokens", 0)
                    tokens.output_tokens = usage.get("output_tokens", 0)
                    tokens.session_id = data.get("session_id", "") or tokens.session_id
                    break
                if "usage" in data and data["usage"].get("input_tokens"):
                    tokens.input_tokens = data["usage"]["input_tokens"]
                    tokens.output_tokens = data["usage"].get("output_tokens", 0)
                    break
            except (json.JSONDecodeError, KeyError):
                continue

        if not tokens.input_tokens:
            tokens.output_tokens = len(stdout) // 4
            tokens.input_tokens = len(prompt) // 4

        tokens.calculate_cost()

        if returncode != 0:
            tokens.error = stderr[-500:] if stderr else f"Exit code {returncode}"
            tokens.success = False
            await tracker.step_fail("agent:updater", tokens.error[:100])
            await tracker.fail(f"Update failed:\n{tokens.error[:500]}")
            report.add(tokens)
            return
        else:
            tokens.success = True
            report.add(tokens)
            await tracker.step_done("agent:updater",
                                     f"Done in {tokens.duration_seconds:.0f}s | ${tokens.cost_usd:.3f}")
            tracker.log(f"🔧 Updater finished: {tokens.input_tokens + tokens.output_tokens:,} tokens | ${tokens.cost_usd:.3f}")

        # Save report
        if tokens.session_id:
            project.claude_session_id = tokens.session_id
        project.actual_cost_usd += report.total_cost_usd
        project.total_input_tokens += report.total_input_tokens
        project.total_output_tokens += report.total_output_tokens
        await db.save(project)

        # ── Redeploy (zero-downtime swap) ────────
        deployed = await deploy_project(bot, project, tracker)
        if not deployed:
            return

        # Keep Git as the source of truth — push the AI's changes upstream.
        if project.repo_full_name:
            gm = GitManager(project, tracker)
            if await gm.commit_and_push(f"AI update: {instructions[:72]}"):
                tracker.log("⬆️ Pushed AI changes to GitHub.")

        await bot.send_message(
            chat_id=project.telegram_chat_id,
            text=(
                f"📊 <b>Update Report</b>\n\n"
                f"✅ <b>updater</b>: {tokens.input_tokens + tokens.output_tokens:,} tokens | "
                f"${tokens.cost_usd:.3f} | {tokens.duration_seconds:.0f}s\n\n"
                f"💰 <b>Total: ${tokens.cost_usd:.3f}</b>"
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception(f"Update pipeline failed for {project.slug}")
        await tracker.fail(f"Update error: {str(e)}")


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
