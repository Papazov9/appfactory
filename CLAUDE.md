# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

AppFactory is a Telegram bot that turns natural-language project briefs into fully deployed web applications. Users describe an app via text or voice in Telegram, the bot estimates cost/complexity, then a multi-agent Claude Code pipeline builds, containerizes, and deploys the app behind a Cloudflare Tunnel subdomain.

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

**Pipeline flow:** Telegram message → Orchestrator → Estimator (cost approval) → MultiAgentBuilder → DockerManager → TunnelManager → live URL sent back to user.

### Key modules

- **`bot/main.py`** — Entry point. Registers conversation handlers BEFORE command handlers (order matters for python-telegram-bot).
- **`bot/config.py`** — Env-driven config loaded at module level. Validates on startup.
- **`bot/handlers/conversations.py`** — Two conversation flows: `/new` (text) and `/voice` (voice-to-app with AI extraction of name/type/brief from transcript).
- **`bot/handlers/commands.py`** — All commands protected by `@auth_check` decorator (user ID whitelist).
- **`bot/services/orchestrator.py`** — Central coordinator. Manages the three-phase pipeline: build → containerize → route. Stores pending approvals in a module-level dict (lost on restart).
- **`bot/services/agent_builder.py`** — Multi-specialist agent system (architect → backend → database → frontend → integrator → qa). Agent selection is complexity-driven, not app-type-driven. Saves `.appfactory_checkpoint.json` after each agent for resume on failure.
- **`bot/services/builder.py`** — Original single-agent builder (predecessor to agent_builder).
- **`bot/services/docker_manager.py`** — Auto-detects project type (Node/Python/static) to generate Dockerfile. Containers bind to `127.0.0.1:{port}` with `--restart unless-stopped`.
- **`bot/services/tunnel_manager.py`** — Mutates cloudflared YAML config on disk and restarts the systemd service. Config is stateful and accumulates rules across projects.
- **`bot/services/estimator.py`** — Calls Claude API for complexity classification; falls back to keyword/word-count heuristic.
- **`bot/services/transcriber.py`** — OpenAI Whisper API if key available, otherwise local `whisper` CLI via ffmpeg.
- **`bot/services/progress.py`** — Edits a single Telegram message in-place to show build progress. Stores message_id in DB for persistence across restarts.
- **`bot/models/project.py`** — Project dataclass + async SQLite wrapper with auto-migration. Full lifecycle via `ProjectStatus` enum.

### Design patterns

- **Async throughout** — All I/O is async. Long operations (estimation, build pipeline) run via `asyncio.create_task()` so Telegram handlers never block.
- **Dual fallback** — Estimation (Claude API → heuristic), transcription (OpenAI → local whisper). Pipeline never fails due to a missing optional service.
- **Checkpoint/resume** — Each agent writes a checkpoint file. `/rebuild` skips completed agents and resumes from the failure point.
- **Slug-based identity** — Project slug (lowercase, hyphens, max 40 chars) is used for subdomain, directory name, container name, and Docker image tag. Collisions resolved by appending timestamp.
- **Port pool** — Ports 9000-9100 allocated from DB. Docker kills rogue processes on the target port before binding (cleanup from agents accidentally running `npm start`).
- **Agent prompts warn against running long-lived processes** (npm start, python app.py) since these would hang the build subprocess.
- **Token tracking** — Per-agent input/output tokens tracked, cost calculated at Sonnet 4 pricing ($3/$15 per 1M tokens), stored as JSON in the project DB record.
