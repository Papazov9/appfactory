# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

AppFactory is a Telegram bot that turns natural-language project briefs into fully deployed web applications, then lets you keep maintaining them as a developer. Users describe an app via text, voice, or images in Telegram; the bot estimates cost/complexity, then a multi-agent Claude Code pipeline builds, containerizes, and deploys it behind a Cloudflare Tunnel subdomain. After deploy, a project can be pushed to a private GitHub repo (`/repo`), edited by hand and redeployed (`/redeploy`), or changed with AI (`/update`).

**Supported stacks** (locked, so generated code stays conventional/hand-maintainable): Python (FastAPI/Flask + SQLite), Spring Boot (Java + per-project Postgres), Angular SPA (static), and Spring + Angular fullstack. Defined in `bot/services/stacks.py`.

## Commands

```bash
# Run the bot (development)
python -m bot.main

# Run the bot (production via systemd)
sudo systemctl enable --now appfactory-bot

# Deploy latest changes on VPS
./deploy.sh

# Install dependencies
pip install -r requirements.txt
```

## Architecture

**Pipeline flow:** Telegram message → Orchestrator → Estimator (cost approval) → MultiAgentBuilder → `deploy_project` (DockerManager build/run → TunnelManager route → verify) → live URL. Redeploys/updates go through `deploy_project`'s **zero-downtime swap** (build new container on a second port, health-check, flip the route, retire the old one).

**Maintenance loop:** `/repo` pushes the built source to a private GitHub repo (the project dir becomes a working tree tracking `origin`); `/redeploy` does `git fetch && reset --hard` then redeploys (no AI); `/update` runs an AI edit that auto-commits & pushes. GitHub needs `GITHUB_TOKEN` + `GITHUB_OWNER` in env.

### Key modules

- **`bot/main.py`** — Entry point. Registers conversation handlers BEFORE command handlers (order matters for python-telegram-bot).
- **`bot/config.py`** — Env-driven config loaded at module level. Validates on startup.
- **`bot/handlers/conversations.py`** — Conversation flows: `/new` (pick a **stack**, then a multi-message brief that accepts text, voice, AND images), `/voice` (voice-to-app), and `/update <id>` (same multi-message + image collector). Briefs/updates accumulate messages behind a **"✅ Done"** button; images are saved to the project's `design_refs/`. All are `ConversationHandler`s registered in `main.py` BEFORE the plain `CommandHandler`s.
- **`bot/handlers/commands.py`** — All commands `@auth_check`-gated. Management (`/list`, `/status`, `/stop`, `/delete`, `/logs`, `/rebuild`, `/approve`, `/cancel_build`, `/cost`, `/scan`) plus **Git** (`/repo`, `/redeploy`) delegate to `orchestrator.py`.
- **`bot/services/orchestrator.py`** — Central coordinator. `deploy_project()` is the shared containerize→route→verify step with the **zero-downtime swap** for redeploys. Also: `create_repo()`, `redeploy_project()`/`_run_redeploy()`, `update_project()` (small edits → single cached-context `claude` invocation; ≥500 words → full pipeline). `_save_design_refs()` copies attached images into `design_refs/`.
- **`bot/services/stacks.py`** — Single source of truth for the four stacks: keyboard labels, default DB, the agent pipeline per stack, and `stack_rules()` (the per-stack build conventions injected into every agent prompt — port/0.0.0.0/no-long-running-server + framework specifics).
- **`bot/services/agent_builder.py`** — Multi-specialist agents (architect/backend/database/frontend/integrator/qa). **Agent selection is now stack-driven** (`stacks.agents_for_stack`, pruned by detected features), not complexity-driven; complexity only scales max-turns/timeouts. Each agent is a **separate `claude` CLI subprocess** (see below). Captures the Claude `session_id` (for cached-context updates). QA is non-blocking.
- **`bot/services/git_manager.py`** — Per-project git + GitHub: creates the private repo via API, commits/pushes (token embedded in the `origin` URL), `pull_latest()` for redeploy, `commit_and_push()` for AI updates.
- **`bot/services/docker_manager.py`** — **Stack-aware** Dockerfile generation (Python/Spring multi-stage/Angular-via-`serve`/Spring+Angular). Provisions a **per-project Postgres container + network + volume** when `db_kind == postgres`; SQLite uses a named volume. Exposes building blocks (`build_image`, `ensure_runtime`, `run_app`, `await_health`, `finish_health`, container helpers) used by both first-deploy and the zero-downtime swap. `teardown()` removes containers/network/volumes/image.
- **`bot/services/cost_calibration.py`** — Records actual tokens per `(stack, complexity)` (rolling EMA in a JSON file) so estimates sharpen build-over-build instead of using the fixed tier table.
- **`bot/services/builder.py`** — Original single-agent builder (legacy, unused by the pipeline).
- **`bot/services/tunnel_manager.py`** — Mutates cloudflared YAML config on disk and restarts the systemd service. Config is stateful and accumulates rules across projects.
- **`bot/services/estimator.py`** — Claude API complexity classification (heuristic fallback); applies `cost_calibration` and stack-aware agent selection.
- **`bot/services/transcriber.py`** — OpenAI Whisper API if key available, otherwise local `whisper` CLI via ffmpeg.
- **`bot/services/progress.py`** — Edits a single Telegram message in-place to show build progress. Stores message_id in DB for persistence across restarts.
- **`bot/models/project.py`** — Project dataclass + async SQLite wrapper with auto-migration (add new columns to BOTH the `CREATE TABLE` and the migration `ALTER` loop). Stack/Git/runtime fields: `stack`, `db_kind`, `repo_url`, `repo_full_name`, `default_branch`, `claude_session_id`, `last_good_image`, `deploy_port_b`. `app_type` is retained for back-compat and mirrors `stack`.

### How agents actually run

The bot **does not call the Anthropic SDK to build apps** — it shells out to the `claude` Code CLI, once per agent, via `asyncio.create_subprocess_exec` with `cwd` set to the project directory:

```
claude -p <agent_prompt> --dangerously-skip-permissions --max-turns <n> --output-format stream-json --verbose
```

- `--verbose` is **required** alongside `--output-format stream-json` or the CLI errors out.
- `stdout` is streamed line-by-line; tool-use events drive live progress updates and per-agent token counts are parsed from the final stream-json message.
- Per-agent `--max-turns` and timeouts are hardcoded dicts in `agent_builder.py` (e.g. frontend/integrator get the longest budgets).
- `ANTHROPIC_API_KEY` is injected into the subprocess env when configured.

This subprocess model is why agent prompts forbid long-lived processes (`npm start`, `python app.py`) — those would hang the subprocess until its timeout.

### Design patterns

- **Async throughout** — All I/O is async. Long operations (estimation, build pipeline) run via `asyncio.create_task()` so Telegram handlers never block.
- **Dual fallback** — Estimation (Claude API → heuristic), transcription (OpenAI → local whisper). Pipeline never fails due to a missing optional service.
- **Checkpoint/resume** — Each agent writes a checkpoint file. `/rebuild` skips completed agents and resumes from the failure point.
- **Slug-based identity** — Project slug (lowercase, hyphens, max 40 chars) is used for subdomain, directory name, container name, and Docker image tag. Collisions resolved by appending timestamp.
- **Port pool** — Ports 9000-9100 allocated from DB. Docker kills rogue processes on the target port before binding (cleanup from agents accidentally running `npm start`).
- **Agent prompts warn against running long-lived processes** (npm start, python app.py) since these would hang the build subprocess.
- **Token tracking** — Per-agent input/output tokens tracked, cost calculated at Sonnet 4 pricing ($3/$15 per 1M tokens), stored as JSON in the project DB record.
- **Stack-driven, adaptive flow** — The chosen stack defines the maximal agent pipeline; agents that don't apply (e.g. a DB agent for a trivial Python script, or any backend agent for an Angular SPA) are pruned. `stacks.stack_rules()` is injected into every agent prompt so generated code matches what the Dockerfile expects.
- **Zero-downtime deploy / rollback** — `deploy_project` only swaps the live container after the new one passes its health check; a failed build/redeploy leaves the running version untouched. `last_good_image` records the healthy image.
- **Git as source of truth** — Once `/repo` runs, the server project dir tracks `origin`; the only sanctioned changes are `/update` (AI, auto-committed) or push-then-`/redeploy`. `design_refs/` and runtime `data/` are git-ignored.
- **Cached-context updates** — `/update` injects `PLAN.md` + a file map and passes `claude --resume <session_id>` (falling back to a fresh run) so upgrades don't re-establish the whole project context.
- **Calibrated cost** — `cost_calibration` feeds real observed token usage back into the estimate; the first build of a `(stack, complexity)` uses the tier default, later ones use the rolling average.
- **Per-project database** — Spring/fullstack get a dedicated Postgres container (network alias `db`, persistent volume) provisioned by `DockerManager`; Python uses SQLite on a volume; Angular none. DB containers survive app rebuilds.
