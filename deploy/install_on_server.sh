#!/usr/bin/env bash
# Run ON the Linux server (Debian/Ubuntu). Sets up the venv and a systemd
# service that runs the bot 24/7, auto-starts on boot, and restarts on crash.
#
#   APP_DIR=~/eld_alert_bot bash deploy/install_on_server.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/eld_alert_bot}"
SERVICE="algo-eld-bot"
RUN_USER="$(whoami)"

echo "==> App dir : $APP_DIR"
echo "==> Service : $SERVICE  (user: $RUN_USER)"

# 1) System deps
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip rsync
fi
python3 --version

# 2) Virtualenv + Python deps
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
echo "==> venv ready"

# 3) systemd unit
sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=Algo ELD Alert Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py --run
Restart=always
RestartSec=10
User=${RUN_USER}
# basic hardening
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

# 4) Enable + start
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"
sleep 3
sudo systemctl status "$SERVICE" --no-pager | head -12
echo
echo "==> Done. Follow logs with:  journalctl -u ${SERVICE} -f"
