"""ELD Alert Bot — entry point.

Usage:
    python main.py                  # load + validate config, print summary
    python main.py --eld-check      # fetch a live snapshot per configured driver
    python main.py --rules-check    # run the rules engine, print would-be alerts
    python main.py --notify --dry-run  # one cycle, print instead of sending
    python main.py --notify         # one cycle, SEND to Telegram (needs token)
    python main.py --run --dry-run  # continuous loop, print instead of sending
    python main.py --run            # continuous service: poll + SEND every cycle
    python main.py --serve          # scheduler (background) + admin dashboard (foreground)
    python main.py --chats          # print every Telegram chat the bot can currently see

Layer 5 adds the scheduler: a continuous poll loop that commits de-dup state
only after a successful send, isolates per-cycle errors, and stops cleanly on
Ctrl+C / SIGTERM.

Provider auth is two static Partner API keys per company (a platform-wide
Provider key + a per-company Company key) — see src/eld/factor.py.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone

from src.activity_log import ActivityLog
from src.commands import run_command_loop
from src.config import Config, ConfigError, load_config, mask_chat_id, mask_secret
from src.eld import ELDError, build_provider
from src.rules import evaluate_company
from src.registry import GroupRegistry
from src.roster_cache import RosterCache
from src.scheduler import run_cycle, run_forever
from src.state import AlertState
from src.telegram_sender import TelegramSender

# Persisted runtime state. On Railway, set DATA_DIR to a mounted Volume path
# (e.g. /data) so the registry + de-dup + token files survive redeploys/restarts.
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")
REGISTRY_FILE = os.path.join(DATA_DIR, "driver_groups.json")
ACTIVITY_LOG_FILE = os.path.join(DATA_DIR, "activity_log.json")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _warn_if_data_dir_ephemeral() -> None:
    """Loud warning if DATA_DIR looks like a container volume path but isn't a
    real mount — i.e. every redeploy silently wipes registrations + de-dup."""
    if DATA_DIR in (".", "", "./") or not os.path.isabs(DATA_DIR):
        return  # local dev
    try:
        mounted = os.path.ismount(DATA_DIR)
    except OSError:
        mounted = False
    if not mounted:
        logging.warning(
            "=" * 70 + "\n"
            "  DATA_DIR=%s is NOT a mounted volume. Registered driver groups,\n"
            "  de-dup state and the activity log will be LOST on every redeploy.\n"
            "  On Railway: Settings -> Volumes -> add a volume at %s\n"
            + "=" * 70, DATA_DIR, DATA_DIR,
        )


def _make_sender(config: Config, dry_run: bool) -> TelegramSender | None:
    try:
        return TelegramSender(config.secrets.telegram_bot_token, dry_run=dry_run)
    except ValueError as exc:
        print(f"Cannot start Telegram sender: {exc}", file=sys.stderr)
        return None


def _fmt_minutes(values: list[int]) -> str:
    return ", ".join(f"{v}m" for v in values)


def _start_command_loop(config: Config, sender, registry: GroupRegistry,
                        roster: RosterCache, *, lock=None, stop_event=None) -> threading.Thread:
    """Start the fast Telegram command/discovery loop on a daemon thread.

    It becomes the sole getUpdates consumer, so callers must run the scheduler
    with discover=False. `roster` is shared with the scheduler + dashboard.
    """
    thread = threading.Thread(
        target=run_command_loop,
        kwargs=dict(
            sender=sender, registry=registry, roster_cache=roster,
            admin_user_ids=config.admin_user_ids, lock=lock, stop_event=stop_event,
        ),
        daemon=True, name="command-loop",
    )
    thread.start()
    return thread


def print_summary(config: Config) -> None:
    line = "=" * 64
    print(line)
    print("ELD Alert Bot — configuration summary")
    print(line)

    print(f"Companies: {len(config.companies)}  (total drivers: {config.driver_count})")
    for company in config.companies:
        scope = "ALL active" if company.monitor_all_drivers else str(len(company.drivers))
        print(
            f"  • {company.name}  [provider: {company.provider}]  "
            f"drivers: {scope}  "
            f"chat: {mask_chat_id(company.driver_group_chat_id)}  "
            f"enabled: {company.enabled}  "
            f"company_key: {mask_secret(company.company_key) if company.company_key else 'MISSING'}"
        )

    print("\nPlatform Provider-key status:")
    for platform in ("factor", "leader"):
        key = config.secrets.provider_key_for(platform)
        print(f"  {platform}: {mask_secret(key) if key else 'MISSING'}")

    thr = config.low_hours_thresholds_minutes
    print("\nLow-hours thresholds:")
    print(f"  driver_group: {_fmt_minutes(thr.driver_group)}")
    print(f"  team_group:   {_fmt_minutes(thr.team_group)}")

    print("\nIntervals & limits:")
    print(f"  poll_interval_seconds:      {config.poll_interval_seconds}s")
    print(f"  disconnect_realert_minutes: {config.disconnect_realert_minutes}m")
    print(f"  disconnect_stale_minutes:   {config.disconnect_stale_minutes}m")
    print(f"  shift_limit_hours:          {config.shift_limit_hours}h")
    print(
        f"  connection_required_statuses: "
        f"{', '.join(config.connection_required_statuses)}"
    )

    print("\nChat IDs (masked):")
    print(f"  team_group_chat_id: {mask_chat_id(config.team_group_chat_id)}")
    for company in config.companies:
        print(f"  {company.name}: {mask_chat_id(company.driver_group_chat_id)}")

    print("\nAdmin access:")
    if config.admin_user_ids:
        print(f"  ADMIN_TELEGRAM_USER_IDS: {len(config.admin_user_ids)} configured")
    else:
        print("  ADMIN_TELEGRAM_USER_IDS: NOT SET — every Telegram user is "
              "currently treated as an admin")

    print(line)


def eld_check(config: Config) -> int:
    """One-shot live fetch: print a normalized snapshot per configured driver."""
    now = datetime.now(timezone.utc)
    required_labels = set(config.connection_required_statuses)
    rc = 0

    for company in config.companies:
        print(f"\n### {company.name} [{company.provider}] — live driver snapshots ###")
        try:
            provider = build_provider(company, config.secrets)
            result = provider.fetch_snapshots(company.drivers, all_active=company.monitor_all_drivers)
        except ELDError as exc:
            print(f"  ERROR: {exc}")
            rc = 1
            continue

        for snap in result.snapshots:
            c = snap.connection
            if not c.has_vehicle:
                conn = "no vehicle paired"
            else:
                stale = c.staleness_seconds(now)
                stale_txt = f"{int(stale // 60)}m ago" if stale is not None else "unknown"
                conn = f"{c.vehicle_status} (truck {c.vehicle_number}, last {stale_txt})"

            # Preview of the disconnect rule (full logic lives in rules.py later).
            active = snap.duty_status_label in required_labels
            disconnected = c.has_vehicle and (
                c.is_offline or c.is_stale(config.disconnect_stale_minutes, now)
            )
            flag = "  <-- DISCONNECT ALERT" if (active and disconnected) else ""

            print(
                f"  {snap.name:24} {snap.duty_status_label:8} "
                f"drive {snap.hos.drive_hm():>8} | shift {snap.hos.shift_hm():>8} | "
                f"break {snap.hos.break_hm():>8} | conn: {conn}{flag}"
            )

        for name in result.unresolved_names:
            print(f"  {name:24} (could not resolve to a roster driver)")
            rc = 1

    return rc


def rules_check(config: Config) -> int:
    """One-shot: fetch live snapshots, run the rules engine, print would-be alerts.

    Uses a fresh in-memory AlertState (nothing persisted, nothing sent).
    """
    now = datetime.now(timezone.utc)
    state = AlertState()  # in-memory only
    total = 0
    rc = 0

    for company in config.companies:
        print(f"\n### {company.name} [{company.provider}] — alerts that WOULD fire ###")
        try:
            provider = build_provider(company, config.secrets)
            result = provider.fetch_snapshots(company.drivers, all_active=company.monitor_all_drivers)
        except ELDError as exc:
            print(f"  ERROR: {exc}")
            rc = 1
            continue

        alerts = evaluate_company(
            result.snapshots, company=company, config=config, state=state, now=now
        )
        if not alerts:
            print("  (no alerts)")
        for a in alerts:
            total += 1
            target = "DRIVER group" if a.audience == "driver_group" else "TEAM group"
            print(f"\n  ── [{a.kind}] → {target} (chat {mask_chat_id(a.chat_id)}) ──")
            for ln in a.text.splitlines():
                print(f"     {ln}")

        for name in result.unresolved_names:
            print(f"  {name}: (could not resolve to a roster driver)")
            rc = 1

    print(f"\nTotal alerts that would be sent: {total}")
    return rc


def notify(config: Config, dry_run: bool) -> int:
    """Run ONE polling cycle (fetch → evaluate → send → commit-on-success).

    Live mode persists AlertState to disk so re-runs don't re-spam. Dry-run uses
    in-memory state and prints messages instead of sending (so a preview never
    suppresses a later real alert).
    """
    _setup_logging()
    sender = _make_sender(config, dry_run)
    if sender is None:
        return 1

    state = AlertState() if dry_run else AlertState(STATE_FILE)
    registry = GroupRegistry(REGISTRY_FILE)
    activity_log = ActivityLog(None if dry_run else ACTIVITY_LOG_FILE)
    tok = sender.check_token()
    print(f"=== notify cycle — {'DRY-RUN' if dry_run else 'LIVE'} ===")
    print(f"token check: {'OK ' + tok.kind if tok.ok else 'FAILED — ' + str(tok.error)}")
    print(f"registered driver groups: {registry.count()}")

    stats = run_cycle(config, sender, state, registry=registry,
                       activity_log=activity_log)
    print(
        f"\nCycle complete — alerts: {stats.alerts}, sent: {stats.sent}, "
        f"failed: {stats.failed}, registered: {stats.registered}, "
        f"provider_errors: {stats.provider_errors}"
    )
    return 0 if (stats.failed == 0 and stats.provider_errors == 0 and not stats.unresolved) else 1


def run_service(config: Config, dry_run: bool) -> int:
    """Start the continuous scheduler loop (Ctrl+C to stop)."""
    _setup_logging()
    if not dry_run:
        _warn_if_data_dir_ephemeral()
    sender = _make_sender(config, dry_run)
    if sender is None:
        return 1

    tok = sender.check_token()
    if not tok.ok:
        print(f"token check FAILED — {tok.error}", file=sys.stderr)
        return 1

    state = AlertState() if dry_run else AlertState(STATE_FILE)
    registry = GroupRegistry(REGISTRY_FILE)
    activity_log = ActivityLog(None if dry_run else ACTIVITY_LOG_FILE)

    # Live mode: a dedicated thread long-polls Telegram so commands/assignment
    # respond in ~1-2s; the scheduler then skips its own getUpdates step. The
    # command loop is the sole registry writer here, so it needs no shared lock;
    # the scheduler only reads the registry (iteration-safe).
    roster = RosterCache(load_config)
    command_loop = not dry_run
    if command_loop:
        _start_command_loop(config, sender, registry, roster, lock=threading.RLock())

    run_forever(config, sender, state, registry=registry,
                activity_log=activity_log, discover=not command_loop,
                roster_cache=roster)
    return 0


def discover(config: Config) -> int:
    """One-shot: process pending Telegram updates and register driver groups."""
    _setup_logging()
    sender = _make_sender(config, dry_run=False)
    if sender is None:
        return 1
    registry = GroupRegistry(REGISTRY_FILE)
    from src.registry import Candidate, discover_groups

    candidates: list[Candidate] = []
    for company in config.companies:
        try:
            provider = build_provider(company, config.secrets)
            result = provider.fetch_snapshots(
                company.drivers, all_active=company.monitor_all_drivers
            )
        except ELDError as exc:
            print(f"  ERROR: {exc}")
            continue
        candidates += [
            Candidate(s.driver_id, s.name, s.username, s.connection.vehicle_number)
            for s in result.snapshots
        ]

    print(f"Scanning Telegram updates against {len(candidates)} drivers...")
    newly = discover_groups(sender, registry, candidates)
    for name, chat_id, how, title in newly:
        print(f"  ✅ {name}  ←  «{title}»  (by {how}, chat {chat_id})")
    print(f"\nNewly registered: {len(newly)} | total registered: {registry.count()}")
    return 0


def chats(config: Config) -> int:
    """Print every Telegram chat the bot can currently see (id, type, title).

    Non-destructive: reads pending updates with offset 0, so it never acks them
    (the scheduler / --discover still see the same updates). Send a message in a
    group AFTER adding the bot, then run this to copy that group's chat id into
    config.yaml.
    """
    sender = _make_sender(config, dry_run=False)
    if sender is None:
        return 1
    tok = sender.check_token()
    if not tok.ok:
        print(f"token check FAILED — {tok.error}", file=sys.stderr)
        return 1
    print(f"bot: {tok.kind}")

    updates = sender.get_updates(offset=0)
    if not updates:
        print(
            "\nNo chats visible. Add the bot to the group, then send any message "
            "in that group (or type /admin), and run this again."
        )
        return 1

    seen: dict[str, dict] = {}
    for u in updates:
        for key in ("message", "my_chat_member", "edited_message", "channel_post"):
            chat = (u.get(key) or {}).get("chat")
            if chat and str(chat.get("id")) not in seen:
                seen[str(chat["id"])] = chat

    print(f"\n{len(seen)} chat(s) the bot can see:")
    for cid, chat in seen.items():
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
        print(f"  chat_id: {cid:>16}   type: {chat.get('type'):12}   {title}")
    print("\nPut the group's chat_id into config.yaml "
          "(team_group_chat_id / a company's driver_group_chat_id).")
    return 0


def run_dashboard(config: Config) -> int:
    """Start the scheduler on a background thread + serve the admin dashboard.

    This is the Railway --serve mode: one process, one public web service.
    Requires DASHBOARD_ADMIN_PASSWORD + DASHBOARD_SECRET_KEY in the environment.
    """
    _setup_logging()
    _warn_if_data_dir_ephemeral()
    sender = _make_sender(config, dry_run=False)
    if sender is None:
        return 1
    tok = sender.check_token()
    if not tok.ok:
        print(f"token check FAILED — {tok.error}", file=sys.stderr)
        return 1

    for name in ("DASHBOARD_ADMIN_PASSWORD", "DASHBOARD_SECRET_KEY"):
        if not os.environ.get(name):
            print(f"--serve requires {name} to be set in the environment", file=sys.stderr)
            return 1

    try:
        from src.dashboard.app import create_app
        from src.dashboard.runtime import RuntimeContext
        from waitress import serve
    except ImportError as exc:
        print(f"--serve requires Flask + waitress: {exc}", file=sys.stderr)
        return 1

    state = AlertState(STATE_FILE)
    registry = GroupRegistry(REGISTRY_FILE)
    activity_log = ActivityLog(ACTIVITY_LOG_FILE)
    roster = RosterCache(load_config)
    runtime = RuntimeContext(
        config_path="config.yaml", env_path=".env",
        registry=registry, state=state, activity_log=activity_log,
        roster_cache=roster,
    )

    def _on_cycle(stats):
        runtime.last_cycle_stats = stats
        runtime.last_cycle_at = datetime.now(timezone.utc)

    stop_event = threading.Event()
    scheduler_thread = threading.Thread(
        target=run_forever,
        kwargs=dict(
            config=config, sender=sender, state=state, registry=registry,
            activity_log=activity_log, roster_cache=roster,
            config_loader=load_config, on_cycle=_on_cycle,
            stop_event=stop_event, discover=False,
        ),
        daemon=True, name="scheduler",
    )
    scheduler_thread.start()

    # Fast Telegram command/assignment loop — sole getUpdates consumer. Shares
    # runtime.lock with the dashboard's registry edits; the scheduler only reads
    # the registry (discover=False), so it holds no lock during a cycle and a
    # command never waits on a DriveHOS fetch.
    _start_command_loop(config, sender, registry, roster, lock=runtime.lock,
                        stop_event=stop_event)

    app = create_app(runtime)
    port = int(os.environ.get("PORT", 8000))
    print(f"Dashboard serving on 0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port)
    return 0


def main(argv: list[str]) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print("Failed to load configuration.\n", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 1

    print_summary(config)
    print("Config OK")

    if "--eld-check" in argv:
        return eld_check(config)
    if "--rules-check" in argv:
        return rules_check(config)
    if "--notify" in argv:
        return notify(config, dry_run="--dry-run" in argv)
    if "--run" in argv:
        return run_service(config, dry_run="--dry-run" in argv)
    if "--discover" in argv:
        return discover(config)
    if "--chats" in argv:
        return chats(config)
    if "--serve" in argv:
        return run_dashboard(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
