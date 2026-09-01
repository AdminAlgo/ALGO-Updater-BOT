# Claude Code Handoff: ELD Alert Bot

## Project goal

This Python service polls ELD data and sends Hours-of-Service alerts to Telegram driver groups. It must be reliable, conservative, and safe with credentials.

## 2026-09-01: API-layer review — hardening, no auth or endpoint changes

Reviewed the Partner API integration against the live spec
(`https://api.drivehos.app/partner/swagger/doc.json`, re-fetched today). The
auth model and the three endpoints in use are **correct and unchanged**: both
`X-API-Provider-Key` and `X-API-Company-Key` are required on every `/v2/*`
call, and the `limit`/`page` query params plus the
`data`/`total_pages`/`status_code` envelope fields the client reads all match
the spec exactly. Nothing about the data-fetch path was replaced.

What was fixed (all defensive; `tests/test_factor_client.py`,
`tests/test_roster_cache.py`, plus new cases in `tests/test_config.py`):

1. **Cycle-killing crashes on odd payloads.** `scheduler.run_cycle` isolates
   only `ELDError` per company, so any other exception aborts the whole cycle
   for *every* company, including the send phase. Three payload shapes could
   do that, and both companies run `monitor_all_drivers: true`, which is the
   path that had no guard at all:
   - a roster row without `driver_id` → `KeyError` (now skipped + logged; a
     name-matched row without an id counts as unresolved);
   - a non-mapping row inside `data` → `AttributeError` (now filtered in
     `_get_all`);
   - a non-int HOS value like `"3600"` → `ValueError` (now `_as_int`, → 0).
2. **`build_provider` with a missing key** raised a `TypeError` from inside
   urllib; it now raises `ELDError`, which callers already isolate per company.
3. **Retry policy widened** from 429-only to 429/502/503/504 plus network
   errors (same backoff, `Retry-After` honoured). 500 deliberately still does
   not retry. Pagination gained a 50-page cap so a bogus `total_pages` can't
   loop forever.
4. **`RosterCache` held its lock across the network fetch** — three HTTP calls
   per company, each up to the timeout plus backoff. The command loop calls
   `get()` every iteration and the scheduler calls `prime()` every cycle, so a
   slow DriveHOS response stalled the scheduler behind it. The fetch now runs
   with the lock released, with an in-flight flag so concurrent `get()`s serve
   the cache instead of stacking fetches. It also merges per company now: a
   company whose fetch fails keeps its previous roster (as the docstring
   always claimed) instead of vanishing, while a company that got disabled is
   dropped.
5. **Config validation** now rejects credentials that are still `.env.example`
   placeholder text (`REPLACE_ME`, `your-…`, `…-here`) for enabled companies
   and in-use providers, and requires the base URLs to be absolute `https://`
   — both keys travel as request headers. This turns the current live symptom
   (`FACTOR_API_KEY` placeholder → silent 401 every cycle forever) into one
   clear startup message naming the variable. Disabled companies are exempt,
   same isolation principle as before.

**The open blocker is unchanged and is not a code problem:** real
`FACTOR_API_KEY` / `LEADER_API_KEY` Provider keys are still needed. Both
Company keys are real. Nothing else stands between this bot and live data.

## 2026-08-26: reverted to real Partner API keys — bearer-token auth removed entirely

The whole 2026-08-21/22 bearer-token detour (`BearerSession`/`SessionManager`,
per-platform login-session tokens, the DevTools capture hunt) is **gone**.
Reason: it was only ever a workaround for not having real Partner API keys —
the user found that WWH's Factor "System Manager" account has its own **Key
Management** screen (Note: "BOT") issuing a real, non-expiring
`X-API-Company-Key`, and got GOWIN's equivalent too. Confirmed via the actual
Partner API spec (`https://api.drivehos.app/partner/swagger/doc.json`, OAS
2.0 — the rendered Swagger UI is a JS SPA and won't show content through a
plain fetch, but the underlying `doc.json` is a normal JSON file) that every
`/v2/*` endpoint requires **two** headers together, always:
- `X-API-Provider-Key` — platform-wide (one per platform: Factor, Leader)
- `X-API-Company-Key` — per company (WWH, GOWIN)

This is the pre-2026-08-21 model, fully restored:
- `src/eld/session.py` **deleted**. `src/eld/factor.py`'s `FactorELD` now
  takes `provider_key`/`company_key` directly and sends both headers on every
  request — no session, no refresh, no expiry (these keys don't expire).
  `src/eld/leader.py` unchanged (still just inherits `FactorELD`).
- `src/eld/__init__.py`: `build_provider(company, secrets)` — the `sessions`
  parameter is gone from this and every caller (`main.py`, `src/scheduler.py`,
  `src/dashboard/views/drivers.py`).
- `src/config.py`: `Secrets.factor_api_key`/`leader_api_key` (provider keys)
  and `Company.company_key_env`/`company_key` are **back**. Validation is
  conditional/isolated, same principle as before: a provider key is only
  required if an ENABLED company actually uses that provider; a company key
  is only required if that specific company is enabled. `config.yaml` now has
  `company_key_env: WWH_COMPANY_KEY` / `company_key_env: GOWIN_COMPANY_KEY`
  on the two companies.
- Dashboard: the **Credentials** page/blueprint (`src/dashboard/views/credentials.py`,
  its template, the nav link) is **deleted** — it only ever managed bearer
  tokens, which no longer exist. `RuntimeContext.sessions` is gone.
- `tests/test_session.py` + `tests/jwt_helper.py` deleted (tested the removed
  module). `tests/test_config.py` rewritten for the restored key model.

**Current real state of `.env` (2026-08-26):**
- `WWH_COMPANY_KEY` — **real**, confirmed active via the platform's own "list
  my keys" endpoint (`is_revoked: false`).
- `GOWIN_COMPANY_KEY` — **real**, given by the user the same way.
- `FACTOR_API_KEY` / `LEADER_API_KEY` (the **Provider** keys) — **still
  placeholder text**. This is the ONLY remaining gap. `python main.py
  --eld-check` currently returns a clean `401 Unauthorized` from both
  companies — confirmed to be exactly this (both requests now send the
  correct headers; DriveHOS's own error body is `{"status_code":401,...}`,
  not a network/shape problem). The moment a real Provider key lands in
  `.env`, no code changes are needed — it starts working immediately.
- The Provider key is a separate credential from anything in a company's own
  "System Manager" account — it identifies the integration itself, not any
  one company, and is normally issued once by Factor/DriveHOS directly (or
  recoverable from whoever built the original/previous bot, if their old
  `.env` still exists). The user is chasing both leads.

**Do not** reintroduce `BearerSession`/browser-login-token capture for
Factor/Leader — that whole approach is retired now that real, non-expiring
Partner keys exist for this integration.

## 2026-08-22: refresh flow proven for GOWIN/Leader; WWH/Factor still blocked (SUPERSEDED — see 2026-08-26 above)

Manually captured and tested (via ad-hoc scripts, not yet in the repo) the
correct login/refresh call for **GOWIN/Leader**. It worked — authenticated
successfully and called the data endpoints. The empty driver list it
returned is expected, not a bug: GOWIN has zero drivers configured yet
("GOWIN will come soon", per the user). This is the first real proof the
bearer-token approach from 2026-08-21 works end-to-end for at least one
platform.

**WWH/Factor — the platform that actually matters (real drivers) — is still
blocked.** Every captured token for it comes back stale/expired by the time
it's tested. Working theory: Factor and Leader may require separately
captured logins even though they share the same DriveHOS backend — getting a
fresh Leader token does not mean the Factor token is fresh. `tokens_leader.json`
is newer than `tokens_factor.json` on disk, consistent with this. The single
next unblocking step is a **fresh login captured specifically from the
WWH/Factor driver-list page** (not the Leader one) via browser DevTools —
same capture method that worked for GOWIN, just needs to be done from the
Factor side.

Remaining work (~10% of the project) once that capture is in hand:
1. Get the same proof-of-connection working for WWH/Factor (currently
   blocked on the above).
2. Wire the proven method into `BearerSession._do_refresh_http_call`
   (`src/eld/session.py`) — today's success was manual test scripts, not yet
   integrated into the running bot.
3. Find/implement the auto-renew path so the session refreshes itself
   without a human repeating a manual login capture every day.

This refines, not replaces, the 2026-08-21 "Open item" below — same
underlying blocker (refresh endpoint contract undiscovered), now confirmed
solvable for Leader and narrowed down to a Factor-specific credential/login
issue.

## 2026-08-21: auth model rewrite + admin dashboard (supersedes most of below)

The user decided per-company Partner API keys (`X-API-Provider-Key` +
`X-API-Company-Key`) should be retired entirely, in favor of **one
bearer-token session per platform** (Factor, Leader) — a JWT
`access_token`/`refresh_token` pair obtained by logging into each platform's
web app, shared across every company under that platform. Reasons given:
fewer credentials to distribute/manage, and it's the auth model the user
actually has working access to (per-company Partner keys were never actually
issued/working — see the 2026-08-21 401 investigation below, which is what
surfaced this).

This is implemented:
- `src/eld/session.py` — `BearerSession` (one per platform, refresh-before-
  expiry via the JWT's own `exp` claim, retry-once-on-401, persists to
  `DATA_DIR/tokens_<platform>.json`) and `SessionManager` (creates/caches one
  `BearerSession` per platform, lazily, never recreated per company or per
  config reload).
- `src/eld/factor.py` now sends `Authorization: Bearer <token>` instead of
  the two `X-API-*` headers; `src/eld/leader.py` is unchanged (still just
  inherits `FactorELD` — same backend, so the bearer swap covers Leader too).
- `src/config.py`: `Company.company_key_env`/`company_key` and
  `Secrets.factor_api_key`/`leader_api_key` are **deleted**, not deprecated.
  `REQUIRED_ENV_VARS` no longer includes `FACTOR_API_KEY`/`LEADER_API_KEY`.
  New optional `Secrets.*_token_seed` fields (first-boot-only bootstrap).
- **Open item:** the refresh endpoint's exact contract (URL, method, body,
  response field names) is NOT wired in yet — nobody has found public
  documentation for it (checked DriveHOS's Partner API swagger; only the
  three Partner endpoints are documented there, not this login/refresh
  flow). `BearerSession._do_refresh_http_call` raises `NotImplementedError`
  until the user captures the real request from browser DevTools (Network
  tab, login to app.drivehos.app / app.leadereld.com) and it gets filled in.
  Until then, the bot works only as long as the seeded access token is
  valid (short-lived — same-day expiry observed) — **it will start failing
  with `Unauthorized`/`NotImplementedError` once that expires**, and needs
  either the refresh endpoint wired in, or fresh tokens pasted into the
  dashboard's `/credentials` page (`SessionManager.set_manual` /
  `BearerSession.manual_override`).

Also added: an admin web dashboard (`src/dashboard/`, Flask, served by the
new `python main.py --serve` mode) — companies (enable/disable/add, writes
`config.yaml` directly with validate-before-write), drivers (wraps the
existing `GroupRegistry`, same calls the `/add`/`/remove` Telegram commands
already make), status (last cycle stats + a new `src/activity_log.py`
recent-alerts ring buffer), and credentials (token expiry status + manual
paste-in recovery). Single shared admin login (`DASHBOARD_ADMIN_PASSWORD`),
no per-user accounts. The Telegram admin commands are **unchanged** and keep
working — the dashboard is additive, not a replacement. `--run` (worker-only,
no dashboard) still works exactly as before and doesn't import Flask.

Railway deployment changed: `Dockerfile`/`railway.json` now run
`python main.py --serve`, which makes this a public web service (Railway
assigns a domain) instead of a headless worker — see `RAILWAY.md`. Access is
gated only by `DASHBOARD_ADMIN_PASSWORD`; treat the dashboard URL as
sensitive.

**Everything below this section describes the OLD per-company-key model and
the Leader "Option A vs Option B" investigation that led here — kept for
history, but don't follow the old `.env` variable names or `company_key_env`
config field, they no longer exist in the code.**

## Original project note

The project originally supported only the Factor/DriveHOS Partner API.
Leader ELD was believed to use the same backend, but its authentication and
company access needed verifying before enabling it — see "Leader
integration" below for how that played out (confirmed same backend, but the
credentials that actually turned out to be available were bearer tokens, not
Partner API keys, which is what triggered the rewrite above).

## Current status

- Main entry point: `python main.py`
- Continuous worker (no dashboard): `python main.py --run`
- Scheduler + admin dashboard: `python main.py --serve`
- Safe preview: `python main.py --notify --dry-run`
- Live one-cycle notification: `python main.py --notify`
- Live ELD snapshot check: `python main.py --eld-check`
- Rule preview: `python main.py --rules-check`
- Configuration is loaded from `.env` and `config.yaml`.
- `.env` is ignored by Git and must never be printed, committed, or included in chat.
- WWH/Factor is the active company in `config.yaml`.
- GOWIN/Leader is currently disabled with `enabled: false`.

## Architecture

- `main.py`: CLI entry point, service startup, `--serve` dashboard mode.
- `src/config.py`: loads and validates `.env` plus `config.yaml`.
- `src/storage.py`: shared atomic tmp+replace JSON write/read helper.
- `src/eld/base.py`: normalized ELD models (`DriverSnapshot`, `HosTimers`, `ConnectionState`), `ELDError`/`Unauthorized`.
- `src/eld/session.py`: `BearerSession`/`SessionManager` — per-platform bearer-token lifecycle.
- `src/eld/factor.py`: DriveHOS client (bearer auth).
- `src/eld/leader.py`: inherits the Factor client unchanged — same backend.
- `src/eld/__init__.py`: provider factory (`build_provider(company, secrets, sessions)`).
- `src/rules.py`: low-hours, shift-violation, disconnect, and welfare rules.
- `src/scheduler.py`: polling loop, error isolation, send-then-commit behavior, optional config hot-reload + shared cycle lock (for the dashboard).
- `src/telegram_sender.py`: Telegram Bot API delivery and token check.
- `src/registry.py`: maps drivers to Telegram groups and handles group discovery/commands.
- `src/state.py`: persistent alert de-duplication state.
- `src/activity_log.py`: recent-alerts ring buffer for the dashboard.
- `src/log_image.py`: optional HOS ring-card image generation.
- `src/dashboard/`: admin web dashboard (Flask) — see the 2026-08-21 section above.

## Factor/DriveHOS API facts

Swagger UI (JS SPA — fetching this URL directly returns an empty shell, not
useful for automated inspection):

`https://api.drivehos.app/partner/swagger/index.html#/`

The underlying spec (OAS 2.0, a plain JSON file — fetch THIS, not the URL
above, to inspect endpoints/headers programmatically):

`https://api.drivehos.app/partner/swagger/doc.json`

Confirmed from that spec: every endpoint below requires **both**
`X-API-Provider-Key` and `X-API-Company-Key` headers together:

- `GET /v2/drivers`
- `GET /v2/latest-driver-status`
- `GET /v2/latest-vehicle-status`
- (also documented, not yet used by this codebase: `/v2/company-info`,
  `/v2/driver/{driver_id}`, `/v2/vehicles`, `/v2/vehicle/{vehicle_id}`,
  `/v2/vehicle-location-history/{vehicle_id}`)

Do not replace this working data-fetch path without a failing test or
confirmed API change — only the auth layer has changed historically, not
these endpoints.

Real values belong only in `.env` (never `config.yaml`, source, docs, or chat):

```env
FACTOR_API_BASE_URL=https://api.drivehos.app
LEADER_API_BASE_URL=https://api.drivehos.app
FACTOR_API_KEY=...      # Provider key, platform-wide
LEADER_API_KEY=...      # Provider key, platform-wide
WWH_COMPANY_KEY=...     # Company key, referenced from config.yaml
GOWIN_COMPANY_KEY=...   # Company key, referenced from config.yaml
```

Never put credentials in `config.yaml`, Python source, documentation, or Telegram messages.

## Telegram group workflow

1. Create the bot with BotFather.
2. Add the bot as an administrator to each driver group.
3. Group titles should contain the driver full name or truck number for automatic matching.
4. The scheduler processes Telegram updates and registers matching groups.
5. Driver low-hours alerts go to the registered driver group.
6. Central alerts go to `team_group_chat_id`.

Optional admin user IDs are configured as numeric IDs:

```env
ADMIN_TELEGRAM_USER_IDS=123456789
```

Supported commands:

- `/admin`
- `/drivers`
- `/coverage`
- `/add <driver name>`
- `/remove <driver name>`

When `ADMIN_TELEGRAM_USER_IDS` is set, `/add` and `/remove` are restricted to those users. Automatic title-based discovery remains available. The dashboard's **Drivers** page (`--serve`) offers the same assign/remove actions through a web UI, on top of the same `GroupRegistry`.

## Reliability work already completed

- Alert state, driver-group registry, and activity log all use the same atomic tmp+replace write pattern (`src/storage.py`).
- Disabled companies don't block startup when their company key is absent, and don't require a working provider key either (see load_config's isolation checks in `src/config.py`) — a company/platform nobody uses shouldn't block the rest of the app.
- Existing validation checks and admin authorization checks pass.
- Dashboard config-writes validate via a real `load_config()` call against a temp file before replacing `config.yaml`, so a bad edit can't brick the scheduler.

Do not undo these changes.

## Leader integration: CONFIRMED same backend as Factor (2026-08-21)

Leader support confirmed Leader ELD is a white-label of the same DriveHOS
backend as Factor — same API, same base URL, same Partner-API auth model
(now confirmed to be Provider key + Company key, see the 2026-08-26 section
at the top). `src/eld/leader.py` reflects this (`LeaderELD(FactorELD)`, no
overrides). Do not enable GOWIN (`config.yaml` → `enabled: true`) until a
real authenticated `--eld-check` succeeds for it — it's currently blocked on
the same missing `LEADER_API_KEY` as WWH is on `FACTOR_API_KEY`.

## 2026-08-21 status: investigation that led to a (since-reverted) bearer-token detour

`python main.py --eld-check` was failing with `401 Unauthorized` for **both**
WWH/Factor and GOWIN/Leader. The masked config summary showed
`WWH_COMPANY_KEY` ending in `****here` — matching the literal `.env.example`
placeholder suffix `...key-here` — and `FACTOR_API_KEY` was confirmed (via
shape/prefix check, never printing the value) to still be literal placeholder
text. Neither had ever actually been issued/entered at the time. This
prompted a temporary switch to bearer tokens (2026-08-21 through 2026-08-22),
since fully reverted once real Company keys were obtained directly from each
platform's own admin account — see the 2026-08-26 section at the top of this
doc, which is the current state.

## Safe operating sequence

Run from the repository folder:

```powershell
python main.py
python main.py --notify --dry-run
python main.py --eld-check
python main.py --run
```

Do not run live mode until Telegram, API keys, and chat permissions are confirmed.

## Security rules

- Credentials previously pasted into chat are compromised: revoke/rotate them once a durable channel (`.env`) has them.
- Never ask the user to paste a token/key into chat unprompted — if they do anyway, treat it as compromised the same way (get it into `.env` immediately, don't echo it back in any response).
- Never print `.env` or key values; when inspecting `.env` programmatically, check shape (length/prefix/hex-ness) or variable-name presence only, never the value.
- Use masked status checks only (`mask_secret`/`mask_chat_id` in `src/config.py`).
- Do not commit `.env`, runtime state, or generated logs.

## Recommended next tasks

1. [DONE 2026-08-21] Removed a hardcoded Telegram bot token that was sitting as dead code in `main.py`. That token should still be revoked/regenerated via BotFather since it sat in plaintext in a source file, regardless of whether it was ever live.
2. [DONE 2026-08-21, REVERTED 2026-08-26] Per-company Partner API keys were briefly retired in favor of bearer-token sessions; now fully reverted back to Partner API keys (Provider + Company) — see the 2026-08-26 section at the top.
3. **Blocked on the user:** need the real `FACTOR_API_KEY` and `LEADER_API_KEY` (Provider keys — platform-wide, NOT per-company). Both Company keys (WWH, GOWIN) are already real and in `.env`. The user is chasing two leads: (a) whoever has the previous/original bot's `.env` or config, which likely has these values already, and (b) Factor/DriveHOS support directly, who can issue/reissue them. The moment either lands in `.env`, `python main.py --eld-check` should go green with no code changes.
4. [DONE 2026-08-21] Built the admin dashboard (`src/dashboard/`, `--serve` mode) — companies, drivers/groups, status. (The Credentials page was added 2026-08-21 for bearer-token management and removed again 2026-08-26 along with the bearer-token code it managed.)
5. Once both Provider keys land: run `python main.py --eld-check` for both companies, confirm zero 401s and real driver rows, then flip GOWIN's `enabled: true` (via `config.yaml` or the dashboard) and add its Telegram groups.
6. Deploy to Railway with a persistent `/data` volume (`RAILWAY.md`) — note this now exposes a public dashboard URL, protected by `DASHBOARD_ADMIN_PASSWORD`.
7. Add automated unit tests for registry matching and provider response normalization before major changes (config validation already has coverage in `tests/test_config.py`).
