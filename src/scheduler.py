"""Polling scheduler — the always-on service loop.

`run_cycle` runs ONE poll: for each company, fetch snapshots, evaluate rules,
send each alert, and commit its de-dup state ONLY after a successful send (so a
failed send retries next cycle instead of vanishing). State is persisted after
each cycle.

`run_forever` repeats run_cycle every `poll_interval_seconds`, isolating errors
so one bad cycle (or one unreachable provider) never kills the loop, and shutting
down cleanly on Ctrl+C / SIGTERM.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .eld import ELDError, build_provider
from .rules import evaluate_company

log = logging.getLogger("eld_alert_bot")


@dataclass
class CycleStats:
    sent: int = 0
    failed: int = 0
    alerts: int = 0
    provider_errors: int = 0
    registered: int = 0
    unresolved: list[str] = field(default_factory=list)


def run_cycle(config, sender, state, now: datetime | None = None, registry=None,
              activity_log=None, discover: bool = True, roster_cache=None) -> CycleStats:
    """Execute one polling cycle. Returns stats; persists state at the end.

    Order: fetch all snapshots → (registry) discover/match driver groups from
    Telegram updates → evaluate rules → send → commit de-dup on success.

    ``discover=False`` skips the Telegram getUpdates step — set it when a
    dedicated command loop (commands.run_command_loop) owns Telegram polling, so
    the two don't both call getUpdates (Telegram returns HTTP 409 to one).
    """
    now = now or datetime.now(timezone.utc)
    stats = CycleStats()

    # 1) Fetch snapshots for every company.
    fetched = []  # list of (company, result)
    for company in config.companies:
        if not getattr(company, "enabled", True):
            continue  # company paused in config — no polling, no alerts
        try:
            provider = build_provider(company, config.secrets)
            result = provider.fetch_snapshots(
                company.drivers, all_active=company.monitor_all_drivers
            )
        except ELDError as exc:
            stats.provider_errors += 1
            log.error("provider error for %s: %s", company.name, exc)
            continue
        if result.unresolved_names:
            stats.unresolved.extend(result.unresolved_names)
            log.warning("%s: unresolved driver names: %s",
                        company.name, ", ".join(result.unresolved_names))
        fetched.append((company, result))
        # Share this fetch with the command loop + dashboard so they don't each
        # hit DriveHOS on the same provider key.
        if roster_cache is not None:
            roster_cache.prime(company.name, result.snapshots)

    # 2) Group auto-discovery: match new Telegram groups to drivers.
    #    Skipped when a dedicated command loop owns Telegram polling.
    if registry is not None and discover:
        from .registry import Candidate, discover_groups
        candidates = [
            Candidate(s.driver_id, s.name, s.username, s.connection.vehicle_number,
                      company.name)
            for company, result in fetched for s in result.snapshots
        ]
        newly = discover_groups(
            sender, registry, candidates, admin_user_ids=config.admin_user_ids
        )
        stats.registered = len(newly)
        for name, chat_id, how, title in newly:
            log.info("registered group for %s (by %s): %s [%s]", name, how, title, chat_id)

    # 3) Evaluate + send per company.
    for company, result in fetched:
        alerts = evaluate_company(
            result.snapshots, company=company, config=config, state=state,
            now=now, registry=registry,
        )
        stats.alerts += len(alerts)
        for alert in alerts:
            res = sender.send_alert(alert)
            if res.ok:
                alert.commit(state, now)  # de-dup only after a confirmed send
                stats.sent += 1
                log.info("sent [%s] %s -> chat %s", alert.kind, alert.driver_name, alert.chat_id)
            else:
                stats.failed += 1
                log.error("send FAILED [%s] %s -> chat %s: %s (will retry next cycle)",
                          alert.kind, alert.driver_name, alert.chat_id, res.error)
            if activity_log is not None:
                from .activity_log import ActivityEntry
                activity_log.record(ActivityEntry(
                    ts=now.isoformat(), kind=alert.kind, company=company.name,
                    driver_name=alert.driver_name, chat_id=str(alert.chat_id),
                    audience=alert.audience, ok=res.ok, error=res.error,
                ))

    state.save()
    if activity_log is not None:
        activity_log.save()
    return stats


def run_forever(config, sender, state, *, registry=None,
                activity_log=None,
                config_loader: Callable[[], object] | None = None,
                on_cycle: Callable[[CycleStats], None] | None = None,
                cycle_lock=None,
                stop_event: threading.Event | None = None,
                max_cycles: int | None = None,
                discover: bool = True,
                roster_cache=None) -> None:
    """Loop run_cycle every poll_interval_seconds until stopped.

    stop_event : set it to request a graceful stop (also wired to SIGINT/SIGTERM).
    max_cycles : stop after N cycles (used by tests); None = run indefinitely.
    config_loader : if given, called at the top of each cycle to reload config
        (e.g. so dashboard edits to config.yaml take effect without a restart).
        A failed reload logs and keeps the previous cycle's config.
    on_cycle : if given, called with each cycle's CycleStats right after it runs
        (e.g. so a dashboard can show last-cycle status).
    cycle_lock : if given, held for the duration of each run_cycle call (e.g. so
        a dashboard editing the shared GroupRegistry doesn't race the scheduler).
    """
    stop = stop_event or threading.Event()

    def _request_stop(signum, _frame):
        log.info("received signal %s — stopping after current cycle", signum)
        stop.set()

    # Only install signal handlers on the main thread (tests run off-thread).
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _request_stop)
            except (ValueError, OSError):  # pragma: no cover
                pass

    interval = config.poll_interval_seconds
    log.info(
        "scheduler started — %d company(ies), poll every %ds%s",
        len(config.companies), interval, " [DRY-RUN]" if sender.dry_run else "",
    )

    cycles = 0
    while not stop.is_set():
        cycles += 1
        try:
            if config_loader is not None:
                try:
                    config = config_loader()
                except Exception:
                    log.exception("config reload failed — keeping previous cycle's config")
            with (cycle_lock or contextlib.nullcontext()):
                stats = run_cycle(config, sender, state, registry=registry,
                                   activity_log=activity_log, discover=discover,
                                   roster_cache=roster_cache)
            if on_cycle is not None:
                on_cycle(stats)
            cov = registry.coverage() if registry is not None else {"tagged": 0, "total": 0}
            log.info(
                "cycle %d done — alerts=%d sent=%d failed=%d registered=%d "
                "tags=%d/%d provider_errors=%d",
                cycles, stats.alerts, stats.sent, stats.failed, stats.registered,
                cov["tagged"], cov["total"], stats.provider_errors,
            )
        except Exception:  # never let one bad cycle kill the loop
            log.exception("unexpected error in cycle %d", cycles)

        if max_cycles is not None and cycles >= max_cycles:
            break
        # Interruptible sleep so Ctrl+C / SIGTERM stops promptly.
        stop.wait(interval)

    log.info("scheduler stopped after %d cycle(s)", cycles)
