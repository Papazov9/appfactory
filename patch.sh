#!/bin/bash
# patch.sh — Apply fixes to running installation
# Run as root on the VPS
set -e

echo "🔧 Applying patches..."

# 1. Add sudoers entry for appfactory user to restart cloudflared
if [ ! -f /etc/sudoers.d/appfactory ]; then
    echo 'appfactory ALL=(ALL) NOPASSWD: /bin/systemctl restart cloudflared, /usr/bin/pkill' > /etc/sudoers.d/appfactory
    chmod 440 /etc/sudoers.d/appfactory
    echo "✅ Added sudoers entry for cloudflared restart"
else
    echo "⏭️ Sudoers entry already exists"
fi

# 2. Make sure appfactory user can write to cloudflared config
chown appfactory:appfactory /etc/cloudflared/config.yml
echo "✅ Fixed cloudflared config permissions"

# 3. Fix ownership of project files
chown -R appfactory:appfactory /opt/appfactory-bot
echo "✅ Fixed file ownership"

# 4. Restart the bot
systemctl restart appfactory-bot
echo "✅ Bot restarted"

sleep 2
systemctl status appfactory-bot --no-pager | head -5

echo ""
echo "🎉 All patches applied!"
