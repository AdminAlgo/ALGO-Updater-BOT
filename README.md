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

## How a driver's group gets linked

Alerts are routed per driver: each driver's alerts go to that driver's own
Telegram group, and a driver with no linked group gets nothing at all. Linking
happens by itself — there is no per-driver command to run.

The bot links a group the first time it sees any message in it, matching on:

1. **the group title** — name words in any order, so `Karimov Aziz`,
   `Aziz.Karimov`, `Каримов Азиз` and `Abib Mohamed` (middle name dropped) all
   resolve to the right driver;
2. **the truck number** in the title, alone or alongside a name word;
3. **whoever writes in the group** — if their Telegram name is a roster
   driver's, that identifies the group even when the title says only `ELD`.

Anything that fits two drivers links nobody: misrouting one driver's hours to
another is worse than a group staying unlinked for another day. Those land on
the dashboard's **Groups** page with their best guess and a single
*Link all suggested groups* button, plus a dropdown for the rest.

The same page shows how many drivers are currently unreachable, and each poll
cycle logs `linked=N/M`. If that number is below the roster, those drivers are
getting no alerts.

> Two settings this depends on: the bot's Telegram privacy mode must be **off**
> (BotFather → `/setprivacy` → Disable) or it never sees group messages, and
> `DATA_DIR` must point at a **mounted volume** or every link is erased on the
> next redeploy. The Groups page warns about the second one.

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

## Staging: SAFE_MODE and the test bot

A staging deploy runs its own bot token with `SAFE_MODE=1`. The scheduler
evaluates every rule exactly as production does, then **withholds** each alert
so a test bot can never message a real driver. Admin commands still reply for
real, which is why `/add` works on staging while alerts stay silent.

Withheld alerts are reported as withheld — never as sent — and their de-dup
state is **not** committed, so nothing is consumed: the moment a chat becomes
reachable, its pending alerts go out on the next cycle.

To watch a test bot actually deliver, list the chats it may reach:

```bash
SAFE_MODE=1
SAFE_MODE_ALLOW_CHATS=-1002345678901,-1002345678902
```

Those chats — and only those — get real messages. Everything else stays
withheld. Get a chat id from `/whois` in the group, or the panel's **Groups**
page. Leave both empty on production.

Each cycle logs `sent=N failed=N withheld=N`, and the dashboard's Status page
shows the withheld count with the same warning. **If a staging bot is silent,
check `withheld` before anything else** — a non-zero count means the rules are
working and only delivery is blocked.

## Is the bot working? (`/hos`)

The alert rules are threshold-driven: nothing is posted until a timer actually
drops to 2 h / 1 h / 30 min, a 14-hour shift is blown, the ELD goes offline, or
the cycle hits 10 h / 5 h. **Silence is the normal state**, which makes a
healthy bot and a broken one look identical from inside a Telegram group.

`/hos` is the difference:

- **`/hos`** inside a driver's group — that driver's live Drive / Shift / Break
  / Cycle remaining, duty status, vehicle connection, where their alerts are
  routed, and when the next warning would fire. Any group member may run it.
- **`/hos <name or truck>`** anywhere — the same for any driver on the roster.
  Admin-only, since it reaches outside the group it was typed in.

It reads the snapshot the scheduler already fetched, so it costs no extra
DriveHOS call. It also names the two silent failure modes outright: a driver
with no linked group, and an ELD with no active shift.

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
