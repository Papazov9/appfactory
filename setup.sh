#!/bin/bash
set -e

echo "================================================"
echo "  🏭 AppFactory Bot — VPS Setup Script"
echo "================================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check() {
    if command -v "$1" &>/dev/null; then
        echo -e "${GREEN}✓${NC} $1 found"
        return 0
    else
        echo -e "${RED}✗${NC} $1 not found"
        return 1
    fi
}

echo "Checking prerequisites..."
echo ""

MISSING=0
check python3 || MISSING=1
check pip3 || MISSING=1
check docker || MISSING=1
check cloudflared || MISSING=1
check claude || { echo -e "${YELLOW}  Install: npm install -g @anthropic-ai/claude-code${NC}"; MISSING=1; }
check curl || MISSING=1

echo ""
if [ "$MISSING" -eq 1 ]; then
    echo -e "${RED}Some prerequisites are missing. Install them first.${NC}"
    echo ""
    echo "Quick install commands:"
    echo "  Docker:      curl -fsSL https://get.docker.com | sh"
    echo "  Cloudflared: See https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/"
    echo "  Claude Code: npm install -g @anthropic-ai/claude-code"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Setup directory
INSTALL_DIR="/opt/appfactory-bot"
echo ""
echo "Installing to ${INSTALL_DIR}..."

if [ -d "$INSTALL_DIR" ] && [ "$(ls -A $INSTALL_DIR)" ]; then
    echo -e "${YELLOW}Directory exists and is not empty.${NC}"
    read -p "Overwrite? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR/"
cd "$INSTALL_DIR"

# Install Python deps
echo ""
echo "Installing Python dependencies..."
pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

# Setup .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo -e "${YELLOW}================================================${NC}"
    echo -e "${YELLOW}  IMPORTANT: Configure your .env file${NC}"
    echo -e "${YELLOW}================================================${NC}"
    echo ""
    echo "Edit ${INSTALL_DIR}/.env and set:"
    echo "  1. TELEGRAM_BOT_TOKEN  (from @BotFather)"
    echo "  2. ALLOWED_USER_IDS    (your Telegram user ID)"
    echo "  3. BASE_DOMAIN         (your domain)"
    echo "  4. TUNNEL_UUID         (from: cloudflared tunnel list)"
    echo ""
    echo "To find your Telegram user ID, message @userinfobot on Telegram."
    echo ""
fi

# Create data directories
mkdir -p data projects

# Setup systemd service
echo "Setting up systemd service..."
cp appfactory-bot.service /etc/systemd/system/
systemctl daemon-reload

echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Edit /opt/appfactory-bot/.env"
echo "  2. Make sure cloudflared tunnel is configured"
echo "  3. Start the bot:"
echo "     sudo systemctl enable --now appfactory-bot"
echo ""
echo "  Or run manually for testing:"
echo "     cd /opt/appfactory-bot && python3 -m bot.main"
echo ""
