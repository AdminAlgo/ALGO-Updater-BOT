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


def parse_ts(entry: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(entry.get("ts", ""))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_parse_ts = parse_ts  # old name, kept for callers/tests


def _bucket_window(bucket: str, now: datetime) -> datetime | None:
    if bucket == "day":
        return now - timedelta(days=1)
    if bucket == "week":
        return now - timedelta(days=7)
    if bucket == "month":
        return now - timedelta(days=30)
    return None  # all_time


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def aggregate(entries: list[dict], now: datetime, total_active_drivers: int,
              linked_drivers: int | None = None) -> dict:
    """Bucket ``entries`` into day / week / month / all_time.

    Per bucket: delivered / failed / withheld sends; how many distinct drivers
    got at least one update (and % of ``total_active_drivers``, the monitored
    roster — plus % of ``linked_drivers`` when given); counts and distinct
    drivers per alert kind; 14-hour violation episodes (first sends only, not
    the reminders) and how many drivers had one; and a per-company table.
    """
    result: dict[str, dict] = {}
    for bucket in _BUCKETS:
        since = _bucket_window(bucket, now)
        total = delivered = failed = withheld = 0
        by_kind: dict[str, dict] = {}
        by_company: dict[str, dict] = {}
        reached: set[str] = set()
        violators: dict[str, dict] = {}
        for e in entries:
            ts = parse_ts(e)
            if ts is None or (since is not None and ts < since):
                continue
            total += 1
            kind = e.get("kind", "?")
            company = e.get("company") or "—"
            did = e.get("driver_id") or ""
            comp = by_company.setdefault(company, {"sent": 0, "failed": 0, "violations": 0,
                                                   "_drivers": set()})
            k = by_kind.setdefault(kind, {"sent": 0, "failed": 0, "_drivers": set()})
            if e.get("suppressed"):
                withheld += 1
                continue
            if not e.get("ok"):
                failed += 1
                k["failed"] += 1
                comp["failed"] += 1
                continue
            delivered += 1
            k["sent"] += 1
            comp["sent"] += 1
            if did:
                reached.add(did)
                k["_drivers"].add(did)
                comp["_drivers"].add(did)
            if kind == "shift_violation" and not e.get("resend"):
                comp["violations"] += 1
                v = violators.setdefault(did or e.get("driver_name", "?"),
                                         {"name": e.get("driver_name") or did,
                                          "company": company, "count": 0})
                v["count"] += 1
        kinds = {kind: {"sent": v["sent"], "failed": v["failed"], "drivers": len(v["_drivers"])}
                 for kind, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["sent"])}
        companies = [{"name": name, "sent": v["sent"], "failed": v["failed"],
                      "violations": v["violations"], "drivers": len(v["_drivers"])}
                     for name, v in sorted(by_company.items(), key=lambda kv: -kv[1]["sent"])]
        violation_count = sum(v["count"] for v in violators.values())
        result[bucket] = {
            "total": total, "delivered": delivered, "failed": failed, "withheld": withheld,
            "by_kind": kinds, "companies": companies,
            "reached": len(reached),
            "reached_pct": _pct(len(reached), total_active_drivers),
            "reached_linked_pct": (_pct(len(reached), linked_drivers)
                                   if linked_drivers is not None else None),
            "violations": violation_count,
            "violation_drivers": len(violators),
            "violation_drivers_pct": _pct(len(violators), total_active_drivers),
            "top_violators": sorted(violators.values(), key=lambda v: -v["count"])[:10],
        }
    return result


def daily(entries: list[dict], now: datetime, days: int = 30) -> list[dict]:
    """Delivered updates and violations per UTC day, oldest first."""
    start = (now - timedelta(days=days - 1)).date()
    series = {start + timedelta(days=i): {"sent": 0, "violations": 0} for i in range(days)}
    for e in entries:
        ts = parse_ts(e)
        if ts is None or not e.get("ok"):
            continue
        day = ts.date()
        if day in series:
            series[day]["sent"] += 1
            if e.get("kind") == "shift_violation" and not e.get("resend"):
                series[day]["violations"] += 1
    peak = max((v["sent"] for v in series.values()), default=0) or 1
    return [{"day": d.isoformat(), "label": d.strftime("%b %d"), **v,
             "pct": round(v["sent"] / peak * 100)} for d, v in sorted(series.items())]
