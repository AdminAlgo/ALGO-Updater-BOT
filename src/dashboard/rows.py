"""Driver rows for the dashboard — one place that turns a live DriverSnapshot
plus its registry settings into what a row shows, so the Drivers page, the
Companies expand-row, Watchlists and the Overview counters can't disagree
about who is "low on time" or "disconnected".

The look follows the ELD platform the dispatchers already use: a duty-status
badge (DR / ON / OFF / SB), truck and connection, the vehicle's location with
"53 seconds ago", and four clocks (Break · Drive · Shift · Cycle) with bars.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.messages import KIND_LABELS, LANGUAGES

STATUS_SHORT = {"DS_D": "DR", "DS_ON": "ON", "DS_OFF": "OFF", "DS_SB": "SB",
                "DS_PC": "PC", "DS_YM": "YM"}

# Full value of each clock, for the bar under it (seconds).
CLOCKS = (
    ("break", "Break", 8 * 3600),
    ("drive", "Drive", 11 * 3600),
    ("shift", "Shift", 14 * 3600),
    ("cycle", "Cycle", 70 * 3600),
)

ACTIVE_DEFAULT = ("Driving", "On Duty", "Yard Move")


def clock_text(seconds: int) -> str:
    """ELD-style "HH:MM" — 7140 -> "01:59"; cycle can run past 24 ("69:40")."""
    s = max(0, int(seconds or 0))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def ago_text(seconds: float | None) -> str:
    if seconds is None:
        return "no data"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''} ago"
    if s < 3600:
        m = s // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if s < 86400:
        h = s // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = s // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def no_active_shift(snap) -> bool:
    """shift=0 and drive=0 with a full break clock is the ELD's "no shift
    started" state, not a driver out of hours (same guard rules.py uses)."""
    h = snap.hos
    return h.shift_seconds <= 0 and h.drive_seconds <= 0 and h.break_seconds >= 8 * 3600 - 300


def is_disconnected(snap, stale_minutes: int, now: datetime | None = None) -> bool:
    c = snap.connection
    return bool(c.has_vehicle and (c.is_offline or c.is_stale(stale_minutes, now)))


def lowest_clock(snap) -> tuple[str, int]:
    """(label, seconds) of the most urgent clock of Drive / Shift / Break / Cycle."""
    h = snap.hos
    options = {"Drive": h.drive_seconds, "Shift": h.shift_seconds,
               "Break": h.break_seconds, "Cycle": h.cycle_seconds}
    return min(options.items(), key=lambda kv: kv[1])


def is_low_time(snap, minutes: int) -> bool:
    if no_active_shift(snap):
        return False
    return lowest_clock(snap)[1] <= minutes * 60


def location_of(snap) -> str:
    veh = (getattr(snap, "raw", None) or {}).get("vehicle") or {}
    loc = (veh.get("calc_location") or "").strip()
    if loc:
        return loc
    lat, lon = veh.get("lat"), veh.get("lon")
    if lat and lon:
        return f"{lat}, {lon}"
    return ""


def speed_of(snap):
    veh = (getattr(snap, "raw", None) or {}).get("vehicle") or {}
    try:
        return int(float(veh.get("speed"))) if veh.get("speed") not in (None, "") else None
    except (TypeError, ValueError):
        return None


def driver_row(company, snap, *, registry, config, now: datetime | None = None,
               state=None, seen=None, records: dict | None = None) -> dict:
    """Everything one table row needs, as plain values for the template."""
    now = now or datetime.now(timezone.utc)
    did = snap.driver_id
    active_statuses = set(getattr(config, "connection_required_statuses", ACTIVE_DEFAULT))
    stale_min = getattr(config, "disconnect_stale_minutes", 30)
    low_min = getattr(config, "watchlist_low_minutes", 30)

    rec = (records if records is not None else registry.all()).get(did) or {}
    chat = rec.get("chat_id") or ""
    dispatch = rec.get("dispatch_chat_id") or ""
    company_kinds = set(getattr(company, "disabled_kinds", None) or ())
    driver_kinds = set(rec.get("disabled_kinds") or ())
    explicit_lang = rec.get("language") or None
    language = explicit_lang or getattr(company, "language", None) or "en"
    paused = bool(rec.get("paused"))

    disconnected = is_disconnected(snap, stale_min, now)
    code = snap.duty_status_code
    label = snap.duty_status_label
    active = label in active_statuses
    no_shift = no_active_shift(snap)
    low_label, low_secs = lowest_clock(snap)
    low = (not no_shift) and low_secs <= low_min * 60

    timers = []
    for key, name, full in CLOCKS:
        secs = getattr(snap.hos, f"{key}_seconds")
        timers.append({
            "key": key, "label": name, "text": clock_text(secs),
            "pct": max(0, min(100, round(secs / full * 100))) if full else 0,
            "low": (not no_shift) and secs <= low_min * 60,
            "zero": (not no_shift) and secs <= 0,
        })

    stale_s = snap.connection.staleness_seconds(now)
    enabled_kinds = [k for k in KIND_LABELS if k not in driver_kinds]
    return {
        "driver_id": did,
        "name": snap.name or snap.username or did,
        "username": snap.username,
        "company": company.name,
        "status_code": code or "",
        "status_short": STATUS_SHORT.get(code or "", (code or "?").replace("DS_", "")[:3]),
        "status_label": label,
        "active": active,
        "truck": snap.connection.vehicle_number or "",
        "has_vehicle": snap.connection.has_vehicle,
        "vehicle_status": snap.connection.vehicle_status or "",
        "speed": speed_of(snap),
        "location": location_of(snap),
        "last_seen": ago_text(stale_s) if snap.connection.has_vehicle else "no vehicle paired",
        "timers": timers,
        "no_shift": no_shift,
        "low": low,
        "low_clock": low_label,
        "low_minutes": max(0, low_secs // 60),
        "low_seconds": low_secs,
        "disconnected": disconnected,
        "rolling": disconnected and label == "Driving",
        # Group routing + settings
        "linked": bool(chat),
        "chat_id": chat,
        "eld_title": (registry.group_title(chat) or rec.get("title") or chat) if chat else "",
        "dispatch_chat_id": dispatch,
        "dispatch_title": (registry.group_title(dispatch) or dispatch) if dispatch else "",
        "language": language,
        "language_name": LANGUAGES.get(language, language),
        "language_explicit": explicit_lang or "",
        "enabled_kinds": enabled_kinds,
        "kinds_csv": ",".join(enabled_kinds),
        "company_off_kinds": sorted(company_kinds),
        "kinds_on": len([k for k in enabled_kinds if k not in company_kinds]),
        "kinds_total": len(KIND_LABELS),
        "paused": paused,
        "blocked": registry.is_blocked(did),
        "is_new": bool(seen is not None and seen.is_new(did, now)),
        "rest_days": (state.rest_days(did, now) if state is not None and not active else None),
    }


def company_rows(runtime, config, now: datetime | None = None) -> list[dict]:
    """Every live driver across enabled companies, one row each."""
    now = now or datetime.now(timezone.utc)
    cache = getattr(runtime, "roster_cache", None)
    records = cache.get_snapshot_records() if cache is not None else []
    by_name = {c.name: c for c in config.companies}
    seen = getattr(cache, "seen", None)
    reg_records = runtime.registry.all()  # one copy for the whole page
    rows = []
    for company_name, snap in records:
        company = by_name.get(company_name)
        if company is None:
            continue
        rows.append(driver_row(company, snap, registry=runtime.registry, config=config,
                               now=now, state=runtime.state, seen=seen,
                               records=reg_records))
    return rows


def summarize(rows: list[dict]) -> dict:
    """Counters shown on company rows and the Overview tiles."""
    return {
        "total": len(rows),
        "low": sum(1 for r in rows if r["low"]),
        "disconnected": sum(1 for r in rows if r["disconnected"]),
        "rolling": sum(1 for r in rows if r["rolling"]),
        "unlinked": sum(1 for r in rows if not r["linked"]),
        "new_unlinked": sum(1 for r in rows if r["is_new"] and not r["linked"]),
        "paused": sum(1 for r in rows if r["paused"]),
        "driving": sum(1 for r in rows if r["status_code"] == "DS_D"),
        "active": sum(1 for r in rows if r["active"]),
    }
