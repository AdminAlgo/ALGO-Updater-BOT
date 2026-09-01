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

    def _refresh(self) -> None:
        config = self._config_loader()
        candidates: list[Candidate] = []
        errors: list[str] = []
        for company in config.companies:
            if not getattr(company, "enabled", True):
                continue
            try:
                provider = build_provider(company, config.secrets)
                result = provider.fetch_snapshots(
                    company.drivers, all_active=company.monitor_all_drivers
                )
            except ELDError as exc:
                errors.append(f"{company.name}: {exc}")
                continue
            for snap in result.snapshots:
                candidates.append(
                    Candidate(
                        snap.driver_id,
                        snap.name,
                        snap.username,
                        snap.connection.vehicle_number,
                        company.name,
                    )
                )
        if candidates:
            self._candidates = candidates
            self._by_company = {}
            for c in candidates:
                self._by_company.setdefault(c.company or "", []).append(c)
            self._fetched_at = time.monotonic()
        self._last_error = "; ".join(errors) or None
        if errors and not candidates:
            log.warning("roster self-refresh failed: %s", self._last_error)

    #: minimum gap between self-initiated fetches, even when the cache is empty
    #: (stops repeated dashboard loads from hammering DriveHOS during an outage).
    _MIN_REFETCH_GAP = 60.0

    def get(self, force: bool = False) -> list[Candidate]:
        """Return the cached roster. Self-fetches only if the cache is empty or
        past its TTL AND the last attempt was long enough ago."""
        with self._lock:
            now = time.monotonic()
            stale = force or not self._candidates or (now - self._fetched_at) >= self._ttl
            if stale and (now - self._last_attempt) >= self._MIN_REFETCH_GAP:
                self._last_attempt = now
                self._refresh()
            return list(self._candidates)

    @property
    def last_error(self) -> str | None:
        return self._last_error
