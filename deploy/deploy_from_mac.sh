#!/usr/bin/env bash
# Run FROM this Mac to push the project to the server and install the service.
#
#   bash deploy/deploy_from_mac.sh user@server-ip
#
# Requires SSH access to the server (key-based recommended). Copies the project
# INCLUDING .env, config.yaml, and the current registry/state so registered
# driver groups and de-dup state carry over.
set -euo pipefail

TARGET="${1:?usage: deploy_from_mac.sh user@host}"
REMOTE_DIR="eld_alert_bot"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Pushing $LOCAL_DIR -> $TARGET:~/$REMOTE_DIR"
rsync -avz \
  --exclude '.venv' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.git' \
  --exclude 'service.log' --exclude 'service.pid' \
  "$LOCAL_DIR"/ "$TARGET:~/$REMOTE_DIR/"

echo "==> Running installer on server"
ssh "$TARGET" "cd ~/$REMOTE_DIR && APP_DIR=\$HOME/$REMOTE_DIR bash deploy/install_on_server.sh"

echo
echo "==> Deployed. IMPORTANT: stop the Mac service so the two don't both poll"
echo "    the same bot token (they would conflict and double-send):"
echo "    launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/com.algoeld.alertbot.plist"
