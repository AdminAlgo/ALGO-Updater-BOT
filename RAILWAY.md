# Deploy on Railway (24/7, UI-managed)

Railway builds the included `Dockerfile` and runs `python main.py --serve` —
the scheduler polls on a background thread while the admin dashboard serves
Railway's public HTTPS domain, password-protected. No SSH, no server
maintenance.

## One-time setup (Railway dashboard)

1. **New Project → Deploy from GitHub repo** → pick this repo
   (`algo-eld-alert-bot`). Railway detects the `Dockerfile` automatically.

2. **Variables** tab → add these (same values as your local `.env`):

   | Variable | Value |
   |---|---|
   | `FACTOR_API_BASE_URL` | `https://api.drivehos.app` |
   | `LEADER_API_BASE_URL` | `https://api.drivehos.app` |
   | `TELEGRAM_BOT_TOKEN` | your @AlgoELDAlertsBot token |
   | `FACTOR_API_KEY` | Factor/DriveHOS Partner Provider key (platform-wide) |
   | `LEADER_API_KEY` | Leader/DriveHOS Partner Provider key (platform-wide) |
   | `WWH_COMPANY_KEY` | WWH's own Company API key |
   | `GOWIN_COMPANY_KEY` | GOWIN's own Company API key |
   | `DASHBOARD_ADMIN_PASSWORD` | password for the admin dashboard login |
   | `DASHBOARD_SECRET_KEY` | random 32-byte hex — `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `DATA_DIR` | `/data` |

   Auth is two static Partner API keys per company (a platform-wide Provider
   key + each company's own Company key) — see `CLAUDE_HANDOFF.md`. Neither
   expires, so there's nothing to refresh or reseed after first boot.

3. **Volumes** → add a Volume, mount path **`/data`**.
   *(Critical — this is where `driver_groups.json` (registered driver groups),
   `alert_state.json` (de-dup), and `activity_log.json` all live. Without it,
   every redeploy forgets all registered groups and re-sends alerts.)*

4. **Networking** → Railway auto-generates a public domain once it detects the
   app is listening on `$PORT` (it will after the first successful deploy).
   That URL is your dashboard — bookmark it, and treat it as sensitive (it's
   protected only by `DASHBOARD_ADMIN_PASSWORD`).

5. **Deploy.** Watch the **Deploy Logs** — you should see
   `Dashboard serving on 0.0.0.0:<port>` and `scheduler started — … poll every
   120s`, then `cycle … done`. Visit the public domain's `/login`.

## IMPORTANT: stop the Mac copy
Once Railway is running, **stop the Mac service** so two bots don't poll the
same Telegram token (they'd conflict and double-send):

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.algoeld.alertbot.plist
```

## Updating later
Push to GitHub → Railway auto-redeploys. State on the `/data` Volume is kept,
so registrations and de-dup survive.

## Notes
- This is now a public web service (dashboard), not a headless worker —
  Railway will assign it a domain. Access is gated by the dashboard login;
  there is no other admin surface exposed.
- `config.yaml` (thresholds, chat IDs, companies) ships in the repo and is
  non-secret — the dashboard's **Companies** page can also edit it directly
  in place (validated before it's written; picked up by the scheduler within
  one poll interval, no redeploy needed).
- Secrets (Provider/Company API keys, Telegram token, dashboard password)
  live only in Railway Variables — never in `config.yaml` or the repo.
