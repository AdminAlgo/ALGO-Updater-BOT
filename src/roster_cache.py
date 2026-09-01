"""Thread-safe cached ELD roster — one fetch shared across the whole process.

The command loop (`/roster`, `/assign`, title-matching), the dashboard Drivers
page, and anything else that just needs "the list of drivers" read from here
instead of each calling DriveHOS independently — one provider key is shared by
every caller AND every company, so uncoordinated fetches trip the rate limit.

The scheduler already fetches full snapshots every cycle; it calls `prime()` so
this cache is normally never the one hitting the API. `get()` only fetches on
its own when the cache is empty or older than `ttl_seconds`, and on failure it
keeps serving the previous roster.
"""

from __future__ import annotations

import logging
import threading
import time

from .eld import ELDError, build_provider
from .registry import Candidate

log = logging.getLogger("eld_alert_bot")


class RosterCache:
    def __init__(self, config_loader, ttl_seconds: int = 600) -> None:
        """`config_loader` is called on each self-refresh so dashboard edits to
        config.yaml (new company, toggled enabled) are picked up."""
        self._config_loader = config_loader
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._candidates: list[Candidate] = []
        self._by_company: dict[str, list[Candidate]] = {}
        self._fetched_at: float = 0.0
        self._last_attempt: float = 0.0
        self._last_error: str | None = None
        #: True while one thread is out on the network; others serve the cache
        #: rather than queueing up behind it on the same fetch.
        self._refreshing = False

    def prime(self, company_name: str, snapshots) -> None:
        """Feed in snapshots the scheduler already fetched this cycle, so this
        cache almost never has to call DriveHOS itself."""
        cands = [
            Candidate(s.driver_id, s.name, s.username,
                      s.connection.vehicle_number, company_name)
            for s in snapshots
        ]
        with self._lock:
            self._by_company[company_name] = cands
            self._candidates = [c for cs in self._by_company.values() for c in cs]
            self._fetched_at = time.monotonic()

    def _fetch(self) -> tuple[dict[str, list[Candidate]], set[str], list[str]]:
        """Fetch every enabled company's roster. Returns (candidates by company,
        enabled company names, errors).

        Pure network work — this MUST run with the lock released. Holding the
        lock across three HTTP calls per company (each up to the request timeout
        plus rate-limit backoff) would block the scheduler's own `prime()` and
        every command-loop `get()` for minutes at a time.
        """
        config = self._config_loader()
        by_company: dict[str, list[Candidate]] = {}
        enabled: set[str] = set()
        errors: list[str] = []
        for company in config.companies:
            if not getattr(company, "enabled", True):
                continue
            enabled.add(company.name)
            try:
                provider = build_provider(company, config.secrets)
                result = provider.fetch_snapshots(
                    company.drivers, all_active=company.monitor_all_drivers
                )
            except ELDError as exc:
                errors.append(f"{company.name}: {exc}")
                continue
            by_company[company.name] = [
                Candidate(
                    snap.driver_id,
                    snap.name,
                    snap.username,
                    snap.connection.vehicle_number,
                    company.name,
                )
                for snap in result.snapshots
            ]
        return by_company, enabled, errors

    #: minimum gap between self-initiated fetches, even when the cache is empty
    #: (stops repeated dashboard loads from hammering DriveHOS during an outage).
    _MIN_REFETCH_GAP = 60.0

    def get(self, force: bool = False) -> list[Candidate]:
        """Return the cached roster. Self-fetches only if the cache is empty or
        past its TTL AND the last attempt was long enough ago."""
        with self._lock:
            now = time.monotonic()
            stale = force or not self._candidates or (now - self._fetched_at) >= self._ttl
            due = stale and (now - self._last_attempt) >= self._MIN_REFETCH_GAP
            if not due or self._refreshing:
                return list(self._candidates)
            self._last_attempt = now
            self._refreshing = True

        try:
            fetched, enabled, errors = self._fetch()
        except Exception as exc:  # config reload failure, unexpected client bug
            log.exception("roster self-refresh failed")
            fetched, enabled, errors = {}, set(), [str(exc)]

        with self._lock:
            self._refreshing = False
            if fetched:
                # Merge per company, and drop only companies that are no longer
                # enabled: a company whose fetch failed this round keeps the
                # roster it already had instead of disappearing from /roster.
                self._by_company = {
                    name: cands
                    for name, cands in self._by_company.items()
                    if name in enabled
                }
                self._by_company.update(fetched)
                self._candidates = [c for cs in self._by_company.values() for c in cs]
                self._fetched_at = time.monotonic()
            self._last_error = "; ".join(errors) or None
            if errors and not fetched:
                log.warning("roster self-refresh failed: %s", self._last_error)
            return list(self._candidates)

    @property
    def last_error(self) -> str | None:
        return self._last_error
