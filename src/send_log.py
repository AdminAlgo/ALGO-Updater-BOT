"""Append-only send log (upgrade spec §3.4) feeding the Statistics page (§4.6).

``activity_log.py``'s 200-entry ring buffer is for the live Status page only
and isn't enough for day/week/month/all-time aggregation. This is a plain
JSONL file, one line per delivery attempt, never rewritten — cheap to scan at
a few hundred sends/day, no database needed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_lock = threading.Lock()

_BUCKETS = ("day", "week", "month", "all_time")


def append(path: str | Path | None, entry: dict) -> None:
    """Add one line. No-op if ``path`` is falsy (e.g. dry-run / CLI tools)."""
    if not path:
        return
    p = Path(path)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def read_all(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    entries: list[dict] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _parse_ts(entry: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(entry.get("ts", ""))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bucket_window(bucket: str, now: datetime) -> datetime | None:
    if bucket == "day":
        return now - timedelta(days=1)
    if bucket == "week":
        return now - timedelta(days=7)
    if bucket == "month":
        return now - timedelta(days=30)
    return None  # all_time


def aggregate(entries: list[dict], now: datetime, total_active_drivers: int) -> dict:
    """Bucket ``entries`` into day/week/month/all_time (spec §4.6).

    Per bucket: total sends, delivered vs failed, broken down by kind, and the
    % of currently-registered drivers who received at least one update in
    that window.
    """
    result: dict[str, dict] = {}
    for bucket in _BUCKETS:
        since = _bucket_window(bucket, now)
        total = delivered = failed = 0
        by_kind: dict[str, int] = {}
        reached: set[str] = set()
        for e in entries:
            ts = _parse_ts(e)
            if ts is None or (since is not None and ts < since):
                continue
            total += 1
            if e.get("ok"):
                delivered += 1
                did = e.get("driver_id")
                if did:
                    reached.add(did)
            else:
                failed += 1
            kind = e.get("kind", "?")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        pct = (len(reached) / total_active_drivers * 100) if total_active_drivers else 0.0
        result[bucket] = {
            "total": total, "delivered": delivered, "failed": failed,
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
            "reached": len(reached), "reached_pct": round(pct, 1),
        }
    return result
