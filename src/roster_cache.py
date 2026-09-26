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
from .registry import Candidate, dedupe_drivers, merge_people

log = logging.getLogger("eld_alert_bot")


class RosterCache:
    def __init__(self, config_loader, ttl_seconds: int = 600, seen=None,
                 registry=None) -> None:
        """`config_loader` is called on each self-refresh so dashboard edits to
        config.yaml (new company, toggled enabled) are picked up. `seen` is an
        optional roster_seen.RosterSeen that records when each driver first
        appears (the dashboard's red NEW label). `registry`, when given, keeps
        the survivor of a two-record driver on whichever id is already linked to
        a group."""
        self._config_loader = config_loader
        self.seen = seen
        self._registry = registry
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._candidates: list[Candidate] = []
        self._by_company: dict[str, list[Candidate]] = {}
        # Full snapshots (HOS timers + connection state), for pages that need
        # more than name/truck — Watchlists (§4.1) and Companies' counters
        # (§4.3). Kept alongside _candidates rather than replacing it: existing
        # callers (roster dropdown, /roster, title matching) only need Candidate.
        self._snapshots_by_company: dict[str, list] = {}
        #: report lines for drivers dropped as cross-company repeats
        self._duplicates: list[str] = []
        self._fetched_at: float = 0.0
        self._last_attempt: float = 0.0
        self._last_error: str | None = None

    def _linked(self, driver_id: str) -> bool:
        return self._registry is not None and self._registry.chat_for(driver_id) is not None

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
            self._snapshots_by_company[company_name] = list(snapshots)
            self._reindex_locked()
            self._fetched_at = time.monotonic()
        if self.seen is not None:
            self.seen.observe(company_name, snapshots)

    def _reindex_locked(self) -> None:
        """Rebuild the flat roster from the per-company lists, one row per
        DRIVER rather than one per (company, driver).

        The scheduler already drops cross-company repeats before priming; this
        repeats the rule for the self-refresh path below and for anything that
        primes this cache directly, so no reader — /roster, /assign, /hos, the
        Drivers page, Watchlists — can ever be handed the same driver twice.

        Two passes, because a driver can be doubled in two different ways: the
        same id returned by two company entries (dedupe_drivers), and one person
        holding two ELD records with different ids (merge_people)."""
        seen: dict[str, str] = {}
        candidates: list[Candidate] = []
        duplicates: list[str] = []
        for company, cands in self._by_company.items():
            kept, dupes = dedupe_drivers(cands, seen, company)
            candidates.extend(kept)
            duplicates.extend(dupes)
        candidates, merged = merge_people(candidates, prefer=self._linked)
        self._candidates = candidates
        self._duplicates = duplicates + merged

    def _refresh(self) -> None:
        config = self._config_loader()
        candidates: list[Candidate] = []
        snapshots_by_company: dict[str, list] = {}
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
            snapshots_by_company[company.name] = list(result.snapshots)
            if self.seen is not None:
                self.seen.observe(company.name, result.snapshots)
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
            self._by_company = {}
            for c in candidates:
                self._by_company.setdefault(c.company or "", []).append(c)
            self._snapshots_by_company = snapshots_by_company
            self._reindex_locked()
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

    def get_snapshot_records(self, force: bool = False) -> list[tuple[str, object]]:
        """(company_name, DriverSnapshot) for every cached driver — Watchlists
        (§4.1) and Companies' per-company counters (§4.3). Shares the same
        staleness/refresh gate as get() so this never triggers an extra fetch
        beyond what get() would already do."""
        with self._lock:
            now = time.monotonic()
            stale = force or not self._snapshots_by_company or (now - self._fetched_at) >= self._ttl
            if stale and (now - self._last_attempt) >= self._MIN_REFETCH_GAP:
                self._last_attempt = now
                self._refresh()
            # Same one-row-per-PERSON rule as the roster above: a snapshot comes
            # back only under the company that owns that driver, and only for an
            # id the roster kept — so a second company entry, or a second ELD
            # record for one person, can't double-count in the counters or
            # Watchlists. An id the roster dropped isn't in `owner` at all.
            owner = {c.driver_id: c.company or "" for c in self._candidates}
            return [(company, snap) for company, snaps in self._snapshots_by_company.items()
                    for snap in snaps if owner.get(snap.driver_id) == company]

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def duplicates(self) -> list[str]:
        """Drivers held back as cross-company repeats — non-empty means two
        company entries are pointing at one ELD account."""
        return list(self._duplicates)
