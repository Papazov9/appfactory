# 🛠️ Complete VPS Setup Guide — AppFactory Bot

This is a step-by-step guide to get AppFactory running on your VPS,
including Cloudflare subdomain delegation and voice message support.

---

## Step 0: Prerequisites on your VPS

SSH into your VPS and install what's needed:

```bash
# Update system
apt update && apt upgrade -y

# Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# Python 3.11+ (usually pre-installed on Ubuntu 22/24)
apt install -y python3 python3-pip

# ffmpeg (needed for voice message transcription)
apt install -y ffmpeg curl

# Node.js 20 (needed for Claude Code)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

# Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Authenticate Claude Code (interactive — do this once)
claude auth
```

---

## Step 1: Delegate a subdomain to Cloudflare

Since your main domain is hosted elsewhere, you'll delegate a subdomain
to Cloudflare. Let's say your main domain is `mybusiness.com` and you
want to use `apps.mybusiness.com` for deployed projects.

### At Cloudflare:

1. **Log into Cloudflare** → Add a site → type `apps.mybusiness.com`
   
   ⚠️ Actually, Cloudflare doesn't support adding just subdomains on
   the free plan. Instead, you have two options:

   **Option A — Best: Add the full domain to Cloudflare (recommended)**
   - Add `mybusiness.com` to Cloudflare
   - Point your registrar's nameservers to Cloudflare
   - Then recreate your existing DNS records in Cloudflare
   - Your existing setup stays intact, you just manage DNS from Cloudflare now

   **Option B — Use a separate cheap domain**
   - Buy a cheap domain (e.g., `myapps.dev` from Namecheap, ~$2/year)
   - Add it to Cloudflare (free plan works)
   - Use this domain exclusively for deployed apps
   - This is the cleanest approach if you don't want to move your main domain

   **Option C — CNAME delegation (advanced)**
   - At your current DNS host, add an NS record:
     `apps.mybusiness.com  NS  → cloudflare nameservers`
   - This requires your DNS host to support subdomain delegation
   - Then add `apps.mybusiness.com` to Cloudflare

   **I recommend Option B** — a $2 domain keeps things completely separate.

2. Once the domain is on Cloudflare, add a **wildcard DNS record**:
   ```
   Type: CNAME
   Name: *
   Target: <YOUR-TUNNEL-UUID>.cfargotunnel.com
   Proxy: ON (orange cloud)
   ```

   Also add the root:
   ```
   Type: CNAME
   Name: @
   Target: <YOUR-TUNNEL-UUID>.cfargotunnel.com
   Proxy: ON (orange cloud)
   ```

---

## Step 2: Set up Cloudflare Tunnel

If you already have a tunnel, skip to "Configure the tunnel."

### Create a tunnel (if needed):

```bash
# Install cloudflared
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
dpkg -i cloudflared.deb

# Login to Cloudflare
cloudflared tunnel login
# This opens a browser — pick your domain

# Create the tunnel
cloudflared tunnel create appfactory
# Note the tunnel UUID printed here!

# Verify
cloudflared tunnel list
```

### Configure the tunnel:

Create/edit `/etc/cloudflared/config.yml`:

```yaml
tunnel: YOUR-TUNNEL-UUID
credentials-file: /root/.cloudflared/YOUR-TUNNEL-UUID.json

ingress:
  # The bot will add entries here automatically!
  # This catch-all MUST be last:
  - service: http_status:404
```

### Run the tunnel as a service:

```bash
cloudflared service install
systemctl enable --now cloudflared

# Verify it's running
systemctl status cloudflared
```

### Get your tunnel UUID:

```bash
cloudflared tunnel list
# Copy the UUID — you'll need it for .env
```

---

## Step 3: Create the Telegram Bot

1. Open Telegram, message **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "My App Factory")
4. Choose a username (e.g., `myappfactory_bot`)
5. **Copy the token** — you'll need it for .env

### Get your Telegram user ID:

1. Message **@userinfobot** on Telegram
2. It replies with your user ID (a number like `123456789`)
3. This is used to restrict who can use the bot (security!)

---

## Step 4: Deploy the bot

```bash
# Extract on VPS (after scp'ing the tar)
cd /opt
tar -xzf appfactory-bot.tar.gz
cd appfactory-bot

# Install Python deps
pip install -r requirements.txt --break-system-packages

# Copy and edit the config
cp .env.example .env
nano .env
```

### Fill in your .env:

```env
# Required
TELEGRAM_BOT_TOKEN=7123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALLOWED_USER_IDS=123456789
BASE_DOMAIN=myapps.dev

# Cloudflare
TUNNEL_UUID=abcd1234-5678-efgh-ijkl-mnopqrstuvwx
CLOUDFLARED_CONFIG_PATH=/etc/cloudflared/config.yml
CLOUDFLARED_CREDENTIALS=/root/.cloudflared/abcd1234-5678-efgh-ijkl-mnopqrstuvwx.json

# Claude (use your Anthropic API key)
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxx

# Voice transcription (pick one):
# Option 1 — OpenAI Whisper API (easiest, ~$0.006/min):
OPENAI_API_KEY=sk-xxxxxxxxxxxxx
# Option 2 — Leave empty and install local whisper (free but uses VPS CPU):
#   pip install openai-whisper --break-system-packages
```

### Test it manually first:

```bash
cd /opt/appfactory-bot
python3 -m bot.main
```

Open Telegram, send `/start` to your bot. If it responds, you're golden.

### Set up as a systemd service:

```bash
cp appfactory-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now appfactory-bot

# Check it's running
systemctl status appfactory-bot

# View live logs
journalctl -u appfactory-bot -f
```

---

## Step 5: Voice transcription setup

You have two options:

### Option A: OpenAI Whisper API (recommended)
- Set `OPENAI_API_KEY` in .env
- That's it. Fast, accurate, costs fractions of a cent.

### Option B: Local Whisper (free, but slower)
```bash
pip install openai-whisper --break-system-packages
# Make sure ffmpeg is installed
apt install -y ffmpeg

# Test it works
whisper --model base --language en /dev/null 2>&1 | head -1
```
Leave `OPENAI_API_KEY` empty in .env and the bot auto-uses local whisper.

---

## How to use it

### Text flow:
1. Send `/new` to the bot
2. Type a project name → pick type → paste your brief
3. Watch the progress bar
4. Get the live URL

### Voice flow (the magic):
1. Just **record a voice message** in the Telegram chat
   - OR send `/voice` first
2. The bot transcribes your audio
3. AI extracts: project name, type, and detailed requirements
4. You review and hit "✅ Build it!"
5. Watch it build → get the live URL

### After a client meeting:
1. Record a voice note summarizing what was discussed
2. Send it to the bot
3. Review the extracted requirements
4. Hit build
5. Send the URL to your client 🎉

---

## Troubleshooting

### "cloudflared: command not found"
```bash
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
dpkg -i cloudflared.deb
```

### "claude: command not found"
```bash
npm install -g @anthropic-ai/claude-code
claude auth
```

### Bot isn't responding
```bash
# Check if it's running
systemctl status appfactory-bot

# Check logs
journalctl -u appfactory-bot -n 50

# Make sure your user ID is in ALLOWED_USER_IDS
```

### App builds but isn't accessible
```bash
# Check the container is running
docker ps | grep appfactory

# Check cloudflared config has the route
cat /etc/cloudflared/config.yml

# Check cloudflared is running
systemctl status cloudflared

# Test locally
curl http://127.0.0.1:9000/  # (use the project's port)
```

### Voice messages aren't working
```bash
# Check ffmpeg
ffmpeg -version

# If using local whisper
whisper --help

# If using OpenAI API, check the key
curl -s https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY" | head -1
```
