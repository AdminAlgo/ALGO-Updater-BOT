"""Thread-safe cached ELD roster — feeds the fast Telegram command loop.

The command loop (src/commands.py) needs the driver roster to answer `/roster`
searches, resolve `/assign <name>`, and title-match new groups — but it must NOT
hit the DriveHOS API on every command (rate limits, latency). So the roster is
fetched at most once per `ttl_seconds` and served from memory in between.

A stale cache is better than a blocked command: if a refresh fails, the previous
snapshot keeps being served and an error is logged.
"""

from __future__ import annotations

import logging
import threading
import time

from .eld import ELDError, build_provider
from .registry import Candidate

log = logging.getLogger("eld_alert_bot")


class RosterCache:
    def __init__(self, config_loader, ttl_seconds: int = 300) -> None:
        """`config_loader` is called on each refresh so dashboard edits to
        config.yaml (new company, toggled enabled) are picked up."""
        self._config_loader = config_loader
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._candidates: list[Candidate] = []
        self._fetched_at: float = 0.0
        self._last_error: str | None = None

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
            self._fetched_at = time.monotonic()
        self._last_error = "; ".join(errors) or None
        if errors and not candidates:
            log.warning("roster refresh failed: %s", self._last_error)

    def get(self, force: bool = False) -> list[Candidate]:
        """Return the cached roster, refreshing if it is older than the TTL."""
        with self._lock:
            age = time.monotonic() - self._fetched_at
            if force or not self._candidates or age >= self._ttl:
                self._refresh()
            return list(self._candidates)

    @property
    def last_error(self) -> str | None:
        return self._last_error
