"""Recent-alerts activity log — for the admin dashboard's status page.

Same persistence contract as AlertState (src/state.py): load-on-construct,
explicit .save(), caller controls when to flush. Capped ring buffer (oldest
entries dropped first) so the file never grows unbounded.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .storage import atomic_write_json, read_json

MAX_ENTRIES = 200


@dataclass(frozen=True)
class ActivityEntry:
    ts: str
    kind: str
    company: str
    driver_name: str
    chat_id: str
    audience: str
    ok: bool
    error: str | None = None


class ActivityLog:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        if self._path and self._path.exists():
            self.load()

    def load(self) -> None:
        if not self._path:
            return
        with self._lock:
            self._entries = read_json(self._path, default=[]) or []

    def save(self) -> None:
        if not self._path:
            return
        with self._lock:
            atomic_write_json(self._path, self._entries)

    def record(self, entry: ActivityEntry) -> None:
        """Append + trim to MAX_ENTRIES. Does not auto-save — call .save()."""
        with self._lock:
            self._entries.append(asdict(entry))
            if len(self._entries) > MAX_ENTRIES:
                self._entries = self._entries[-MAX_ENTRIES:]

    def recent(self, limit: int = 50) -> list[dict]:
        """Newest first."""
        with self._lock:
            return list(reversed(self._entries[-limit:]))
