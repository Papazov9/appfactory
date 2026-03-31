# 🏭 AppFactory — Telegram Bot → AI Agent → Live App Pipeline

A Telegram bot that turns natural-language project briefs into fully deployed web applications on your VPS, accessible via Cloudflare Tunnel subdomains.

## Architecture

```
You (Telegram)
  → Bot receives project brief
  → Claude Code builds the app in a Docker container
  → App spins up on an auto-assigned port
  → Cloudflare Tunnel routes subdomain → container
  → Bot sends you progress updates + live URL
```

## Prerequisites

- **VPS** with Docker & Docker Compose installed
- **Node.js 18+** (for Claude Code)
- **Claude Code CLI** installed and authenticated (`npm install -g @anthropic-ai/claude-code`)
- **Cloudflare Tunnel** (`cloudflared`) installed and authenticated
- **A domain** pointed to Cloudflare (e.g., `yourdomain.com`)
- **Telegram Bot Token** (from @BotFather)
- **Python 3.11+**

## Setup

### 1. Clone & Configure

```bash
cd /opt
git clone <this-repo> appfactory-bot
cd appfactory-bot
cp .env.example .env
# Edit .env with your values
nano .env
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Cloudflare Tunnel

```bash
# If you haven't already created a tunnel:
cloudflared tunnel create appfactory

# Note the tunnel UUID — add it to .env
# Point *.yourdomain.com to the tunnel in Cloudflare DNS:
#   CNAME  *  →  <TUNNEL_UUID>.cfargotunnel.com
```

### 4. Run the Bot

```bash
# Development
python -m bot.main

# Production (systemd)
sudo cp appfactory-bot.service /etc/systemd/system/
sudo systemctl enable --now appfactory-bot
```

## Usage

1. Open your Telegram bot
2. Send `/new` to start a new project
3. Paste your project brief (or voice note transcription)
4. Watch the progress bar as the AI builds your app
5. Receive the live URL when done

### Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new project |
| `/status` | Check current build status |
| `/list` | List all deployed projects |
| `/stop <name>` | Stop a running project |
| `/delete <name>` | Stop and remove a project |
| `/logs <name>` | Get recent logs from a project |
| `/rebuild <name>` | Rebuild an existing project |

## Project Directory Structure

```
/opt/appfactory-bot/
├── bot/
│   ├── main.py              # Entry point
│   ├── config.py             # Configuration loader
│   ├── handlers/
│   │   ├── commands.py       # Telegram command handlers
│   │   └── conversations.py  # Multi-step conversation flows
│   ├── services/
│   │   ├── builder.py        # Claude Code orchestration
│   │   ├── docker_manager.py # Docker container lifecycle
│   │   ├── tunnel_manager.py # Cloudflare Tunnel routing
│   │   └── progress.py       # Progress tracking & Telegram updates
│   └── models/
│       └── project.py        # Project data model
├── projects/                  # Built project directories
├── templates/                 # Dockerfile templates per app type
├── .env.example
├── requirements.txt
└── README.md
```
