#!/bin/bash
# deploy.sh — Run on VPS to pull latest changes and restart the bot
set -e

INSTALL_DIR="/opt/appfactory-bot"
SERVICE_NAME="appfactory-bot"

cd "$INSTALL_DIR"

echo "📥 Pulling latest changes..."
git pull origin main

echo "📦 Installing dependencies..."
pip install -r requirements.txt --break-system-packages -q 2>/dev/null || \
pip install -r requirements.txt -q

echo "🔄 Restarting bot..."
sudo systemctl restart "$SERVICE_NAME"

echo "✅ Deployed! Checking status..."
sleep 2
systemctl status "$SERVICE_NAME" --no-pager | head -5
