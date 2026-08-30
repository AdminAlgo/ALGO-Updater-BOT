# ELD Alert Bot

Automated alerting service that reads Hours-of-Service (HOS) and device data
from two ELD providers (**Factor ELD** and **Leader ELD**) and posts alerts to
Telegram driver groups.

It is **portable** — runnable anywhere. All keys and IDs live in configuration;
nothing is hardcoded.

> **Status:** Core complete (Layers 1–5) — config + **Factor (DriveHOS) ELD
> client** + **rules engine** + **Telegram delivery** + **scheduler** (the
> always-on poll loop). De-dup state is committed only after a successful send;
> per-cycle errors are isolated; Ctrl+C / SIGTERM stop cleanly.
>
> Run the service: `python main.py --run` (or `--run --dry-run` to watch without
> sending). `python main.py --serve` also starts the admin dashboard (see
> below). Leader ELD is confirmed to be a white-label of the same DriveHOS
> backend as Factor (per Leader support) and reuses the same client — it's
> wired up but disabled (`enabled: false`) pending real GOWIN credentials and
> a passing `--eld-check`.

## ELD provider: Factor = DriveHOS

`https://api.drivehos.app`. Auth is two static Partner API keys sent
together on every request: a platform-wide **Provider key**
(`X-API-Provider-Key`, one per platform — Factor, Leader) plus each
company's own **Company key** (`X-API-Company-Key`). Neither expires, so
there's no session/refresh lifecycle — get both keys from Factor/DriveHOS
support and that company's platform admin account, respectively, and drop
them in `.env`.

The client fetches `/v2/drivers`, `/v2/latest-driver-status`, and
`/v2/latest-vehicle-status`, resolves each configured driver to its `driver_id`
by name (or explicit `eld_driver_id`), and joins them into a normalized
`DriverSnapshot` (HOS seconds remaining, duty-status code+label, connection
state). HOS timers are **seconds remaining**; duty codes map as
`DS_D=Driving, DS_ON=On Duty, DS_YM=Yard Move, DS_SB=Sleeper, DS_OFF=Off Duty,
DS_PC=Personal Conveyance`. Connection has no Bluetooth flag — "disconnected" is
inferred from vehicle `status==OFFLINE` or telemetry older than
`disconnect_stale_minutes`.

## Requirements

- Python 3.11+
- Dependencies: `python-dotenv`, `PyYAML`, `Pillow`, `Flask`, `waitress`

## Setup

```bash
# from the eld_alert_bot/ directory
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# create your secrets file from the template, then edit it
cp .env.example .env
# open .env and fill in real Provider/Company API keys, bot token, dashboard password
```

## Configuration

- **Secrets** (Provider + Company API keys, bot token, dashboard password) →
  `.env`. Never committed (see `.gitignore`). Use `.env.example` as the
  template.
- **Non-secret, editable settings** (poll interval, thresholds, companies,
  drivers, chat IDs) → `config.yaml` — also editable from the admin
  dashboard's **Companies** page.

## Run

```bash
python main.py              # load + validate config, print summary
python main.py --eld-check  # also fetch a live snapshot per configured driver
python main.py --serve      # scheduler (background) + admin dashboard (foreground)
```

`--serve` requires `DASHBOARD_ADMIN_PASSWORD` and `DASHBOARD_SECRET_KEY` in
the environment. Visit `http://localhost:8000/login` (or `$PORT`) locally —
Railway assigns a public HTTPS domain automatically (see `RAILWAY.md`).

This loads and validates `.env` + `config.yaml`, prints a readable summary
(companies, providers, driver counts, thresholds, intervals, and masked chat
IDs), and ends with `Config OK` if everything is valid. If anything is missing
or malformed, it prints every problem found and exits non-zero.

## Project layout

```
eld_alert_bot/
  .env.example          # template for secrets
  .gitignore            # excludes .env and __pycache__
  config.yaml           # non-secret settings
  requirements.txt
  main.py               # entry point: load + validate + print summary; --serve
  src/
    config.py           # load .env + config.yaml, validate, expose Config
    storage.py          # shared atomic-JSON write/read helper
    state.py            # alert de-dup / re-alert tracking
    activity_log.py     # recent-alerts ring buffer, for the dashboard
    rules.py            # alert rule evaluation
    telegram_sender.py  # Telegram delivery
    registry.py         # per-driver Telegram group registry + /add /remove commands
    scheduler.py        # polling loop (+ config hot-reload, dashboard hooks)
    eld/
      base.py           # common ELD provider interface
      factor.py         # Factor ELD client (Provider + Company API-key auth)
      leader.py         # Leader ELD client (inherits Factor — same backend)
    dashboard/          # admin web dashboard (Flask, served by --serve)
      app.py, auth.py, runtime.py, config_writer.py
      views/             # companies, drivers, status, health
      templates/, static/
```
