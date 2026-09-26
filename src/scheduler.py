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

from . import send_log
from .eld import ELDError, build_provider
from .registry import dedupe_drivers, merge_people
from .rules import evaluate_company
from .sender_queue import SendQueue

log = logging.getLogger("eld_alert_bot")


@dataclass
class CycleStats:
    sent: int = 0
    failed: int = 0
    # Alerts SAFE_MODE withheld. They are neither delivered nor lost: de-dup is
    # not committed, so each one is re-detected and re-offered every cycle until
    # its chat is allowlisted (SAFE_MODE_ALLOW_CHATS) or SAFE_MODE is turned off.
    suppressed: int = 0
    alerts: int = 0
    provider_errors: int = 0
    registered: int = 0
    unresolved: list[str] = field(default_factory=list)
    queue_drain_seconds: float = 0.0
    # Drivers polled this cycle who have no linked Telegram group. Routing is
    # driver-group-only, so these drivers CANNOT be alerted at all — the number
    # is surfaced here, in the cycle log and on the dashboard because an empty
    # registry once silenced the whole fleet for a day without a single error.
    monitored: int = 0
    unlinked: list[str] = field(default_factory=list)
    # Drivers a second company entry returned again this cycle (same ELD
    # account configured twice). They are polled once, under the first company.
    duplicates: list[str] = field(default_factory=list)


def run_cycle(config, sender, state, now: datetime | None = None, registry=None,
              activity_log=None, discover: bool = True, roster_cache=None,
              send_log_path=None) -> CycleStats:
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
    # driver_id -> the company that already returned that driver this cycle.
    # Two entries on one ELD account hand back the SAME drivers, and a repeat
    # that reaches the rules is a second alert for one person, a roster where
    # /assign sees "2 drivers", and a group title that matches nobody because
    # it looks ambiguous. The first company in config order keeps the driver.
    claimed_by: dict[str, str] = {}
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
        kept, dupes = dedupe_drivers(result.snapshots, claimed_by, company.name)
        if dupes:
            result.snapshots[:] = kept
            stats.duplicates.extend(dupes)
        fetched.append((company, result))
        # Share this fetch with the command loop + dashboard so they don't each
        # hit DriveHOS on the same provider key.
        if roster_cache is not None:
            roster_cache.prime(company.name, result.snapshots)

    # 1a) The other shape of "one driver, two rows": not one id returned by two
    #     companies (handled above), but one PERSON the ELD holds under two
    #     different ids. Both carry real hours, so without this the driver gets
    #     every alert twice, and half of them are computed against the record
    #     nobody linked to a group — which is a send to nowhere. Runs across all
    #     companies at once because the twin can sit in either one. The id that
    #     already has a group wins, so the choice doesn't move between cycles.
    if fetched:
        linked = (lambda did: registry.chat_for(did) is not None) if registry else None
        keep, merged = merge_people([s for _, result in fetched for s in result.snapshots],
                                    prefer=linked)
        if merged:
            keep_ids = {s.driver_id for s in keep}
            for _company, result in fetched:
                result.snapshots[:] = [s for s in result.snapshots if s.driver_id in keep_ids]
            stats.duplicates.extend(merged)

    if stats.duplicates:
        shown = ", ".join(stats.duplicates[:10])
        more = f" (+{len(stats.duplicates) - 10} more)" if len(stats.duplicates) > 10 else ""
        log.warning(
            "%d driver row(s) folded as repeats of a driver already on the "
            "roster — one ELD account configured as two companies, or one "
            "person holding two ELD records: %s%s",
            len(stats.duplicates), shown, more,
        )

    # 1b) Delivery coverage. A driver with no linked group is invisible to every
    #     rule below, so count them before evaluating rather than discovering it
    #     from a silent day with zero sends.
    if registry is not None:
        roster = [(c, s) for c, result in fetched for s in result.snapshots]
        stats.monitored = len(roster)
        stats.unlinked = sorted(f"{s.name} ({c.name})" for c, s in roster
                                if not registry.chat_for(s.driver_id))
        if stats.unlinked:
            shown = ", ".join(stats.unlinked[:10])
            more = f" (+{len(stats.unlinked) - 10} more)" if len(stats.unlinked) > 10 else ""
            log.warning("NO ALERTS POSSIBLE for %d of %d drivers — no linked group: %s%s",
                        len(stats.unlinked), stats.monitored, shown, more)

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

    # 3) Evaluate every company, queue every alert, then drain the queue once.
    #    Detection never blocks on Telegram — the queue enforces rate limits
    #    and retries 429s instead of dropping messages (FIX-7).
    queue = SendQueue(
        sender,
        global_per_second=getattr(config, "send_rate_per_second", 25),
        per_chat_per_minute=getattr(config, "send_rate_per_chat_per_minute", 20),
    )
    for company, result in fetched:
        alerts = evaluate_company(
            result.snapshots, company=company, config=config, state=state,
            now=now, registry=registry,
        )
        stats.alerts += len(alerts)
        for alert in alerts:
            queue.push(alert)

    def _on_result(alert, res):
        if res.ok:
            alert.commit(state, now)  # de-dup only after a confirmed send
            stats.sent += 1
            log.info("sent [%s] %s -> chat %s", alert.kind, alert.driver_name, alert.chat_id)
        elif getattr(res, "suppressed", False):
            # No commit: a withheld alert must stay pending. Committing here is
            # what made SAFE_MODE silently eat alerts AND report them as sent.
            stats.suppressed += 1
            log.warning("WITHHELD [%s] %s -> chat %s: %s — add %s to "
                        "SAFE_MODE_ALLOW_CHATS to receive it for real",
                        alert.kind, alert.driver_name, alert.chat_id, res.error,
                        alert.chat_id)
        else:
            stats.failed += 1
            log.error("send FAILED [%s] %s -> chat %s: %s (will retry next cycle)",
                      alert.kind, alert.driver_name, alert.chat_id, res.error)
        if activity_log is not None:
            from .activity_log import ActivityEntry
            activity_log.record(ActivityEntry(
                ts=now.isoformat(), kind=alert.kind, company=alert.company,
                driver_name=alert.driver_name, chat_id=str(alert.chat_id),
                audience=alert.audience, ok=res.ok, error=res.error,
            ))
        send_log.append(send_log_path, {
            "ts": now.isoformat(), "kind": alert.kind, "company": alert.company,
            "driver_id": alert.driver_id, "driver_name": alert.driver_name,
            "chat_id": str(alert.chat_id), "audience": alert.audience, "ok": res.ok,
            "suppressed": bool(getattr(res, "suppressed", False)),
            # a 14h-violation reminder, not a new episode — Statistics counts
            # violations once per episode
            "resend": bool(getattr(alert, "is_resend", False)),
        })

    drain = queue.drain(on_result=_on_result)
    stats.queue_drain_seconds = drain.drain_seconds
    log.info("send queue drained — detected=%d sent=%d failed=%d withheld=%d in %.1fs",
             drain.detected, drain.sent, drain.failed, drain.suppressed,
             drain.drain_seconds)

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
                roster_cache=None,
                send_log_path=None) -> None:
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
                                   roster_cache=roster_cache, send_log_path=send_log_path)
            if on_cycle is not None:
                on_cycle(stats)
            cov = registry.coverage() if registry is not None else {"tagged": 0, "total": 0}
            log.info(
                "cycle %d done — alerts=%d sent=%d failed=%d withheld=%d registered=%d "
                "linked=%d/%d tags=%d/%d provider_errors=%d drain=%.1fs",
                cycles, stats.alerts, stats.sent, stats.failed, stats.suppressed,
                stats.registered,
                stats.monitored - len(stats.unlinked), stats.monitored,
                cov["tagged"], cov["total"], stats.provider_errors,
                stats.queue_drain_seconds,
            )
        except Exception:  # never let one bad cycle kill the loop
            log.exception("unexpected error in cycle %d", cycles)

        if max_cycles is not None and cycles >= max_cycles:
            break
        # Interruptible sleep so Ctrl+C / SIGTERM stops promptly.
        stop.wait(interval)

    log.info("scheduler stopped after %d cycle(s)", cycles)
