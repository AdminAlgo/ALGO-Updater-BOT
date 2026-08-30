> **Note:** the primary deployment target is now Railway (see `RAILWAY.md`),
> running `python main.py --serve` — scheduler + admin dashboard in one
> process. Everything below still works for a worker-only deployment
> (`--run`, no dashboard); swap `--run` for `--serve` in any `ExecStart`/
> ExecStart-equivalent line below if you also want the dashboard on that host,
> and make sure `DASHBOARD_ADMIN_PASSWORD`/`DASHBOARD_SECRET_KEY` are set and
> the port is reachable.

# Running 24/7 (macOS / launchd)

The bot runs as a launchd **LaunchAgent** that starts at login and restarts
itself if it crashes.

- **Service label:** `com.algoeld.alertbot`
- **Definition:** `~/Library/LaunchAgents/com.algoeld.alertbot.plist`
- **Logs:** `/Users/aero/eld_alert_bot/service.log`
- **De-dup state:** `/Users/aero/eld_alert_bot/alert_state.json`

## Manage it

```bash
UID_NUM=$(id -u)

# Status (running? pid?)
launchctl print gui/$UID_NUM/com.algoeld.alertbot | grep -E "state =|pid ="

# Watch live activity
tail -f /Users/aero/eld_alert_bot/service.log

# Stop now AND on future logins (disable)
launchctl bootout gui/$UID_NUM ~/Library/LaunchAgents/com.algoeld.alertbot.plist

# Start / re-enable
launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/com.algoeld.alertbot.plist

# Restart (e.g. after editing config.yaml or .env)
launchctl kickstart -k gui/$UID_NUM/com.algoeld.alertbot
```

> After changing `config.yaml` or `.env`, run the **restart** command so the bot
> picks up the new values.

## ⚠️ macOS limitation: sleep & login

A LaunchAgent only runs while **this user is logged in** and the Mac is **awake**.
If the Mac **sleeps**, the bot pauses until it wakes. For true always-on:

- Keep the Mac plugged in and prevent sleep:
  `System Settings → Lock Screen / Displays → "prevent automatic sleeping"`,
  or run alongside: `caffeinate -dimsu` (keeps the Mac awake).
- Or host it on an always-on box (a small Linux VPS) — see below.

## Linux server (systemd) — for real always-on hosting

Copy the project to the server, create the venv + `.env`, then:

```ini
# /etc/systemd/system/algo-eld-bot.service
[Unit]
Description=Algo ELD Alert Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/eld_alert_bot
ExecStart=/opt/eld_alert_bot/.venv/bin/python /opt/eld_alert_bot/main.py --run
Restart=always
RestartSec=10
User=eldbot

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now algo-eld-bot
journalctl -u algo-eld-bot -f      # watch logs
```
