"""When each driver first appeared on a company's live roster.

The dashboard flags a driver as NEW — in red, until someone links their
group — for a week after they first show up. That is how "the carrier added 10
drivers and none of them get updates" becomes visible instead of silent.

The very first sweep after this file is created is the baseline: everyone on
the roster then is simply "already here", not new, so switching the feature on
(or a fresh volume) doesn't paint the whole fleet red. Anything that appears
after that grace window is new.

Persisted to ``DATA_DIR/roster_seen.json``; written only when a driver is seen
for the first time, so a normal poll cycle costs no disk write.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import atomic_write_json, read_json

log = logging.getLogger("eld_alert_bot")

#: drivers first seen within this long of the file's creation are the baseline
BASELINE_GRACE = timedelta(minutes=15)
#: how long a driver stays "new"
NEW_FOR = timedelta(days=7)


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class RosterSeen:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._initialized: str | None = None
        self._drivers: dict[str, dict] = {}
        if self._path is not None:
            data = read_json(self._path, default={}) or {}
            self._initialized = data.get("initialized")
            self._drivers = data.get("drivers") or {}

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            atomic_write_json(self._path, {"initialized": self._initialized,
                                           "drivers": self._drivers})
        except OSError as exc:  # a full/readonly disk must not break polling
            log.warning("could not save %s: %s", self._path, exc)

    def observe(self, company: str, snapshots, now: datetime | None = None) -> list[str]:
        """Record every driver in ``snapshots``; returns ids seen for the first time."""
        now = now or datetime.now(timezone.utc)
        added: list[str] = []
        with self._lock:
            if self._initialized is None:
                self._initialized = now.isoformat()
            start = _parse(self._initialized) or now
            baseline = now - start <= BASELINE_GRACE
            for snap in snapshots:
                did = snap.driver_id
                if not did or did in self._drivers:
                    continue
                self._drivers[did] = {"first_seen": now.isoformat(), "company": company,
                                      "name": snap.name, "baseline": baseline}
                added.append(did)
            if added:
                self._save_locked()
        if added and not baseline:
            log.info("%s: %d new driver(s) on the roster", company, len(added))
        return added

    def first_seen(self, driver_id: str) -> datetime | None:
        rec = self._drivers.get(driver_id)
        return _parse(rec.get("first_seen")) if rec else None

    def is_new(self, driver_id: str, now: datetime | None = None) -> bool:
        rec = self._drivers.get(driver_id)
        if not rec or rec.get("baseline"):
            return False
        seen = _parse(rec.get("first_seen"))
        now = now or datetime.now(timezone.utc)
        return seen is not None and now - seen <= NEW_FOR
