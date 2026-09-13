"""Watchlists (upgrade spec §4.1) — read-only lenses over the live RosterCache
snapshot the scheduler already primes every cycle. No new evaluation logic and
no extra ELD fetch: this just filters the same data rules.py already sees.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

from ..auth import login_required

bp = Blueprint("watchlists", __name__, url_prefix="/watchlists")


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    cache = getattr(runtime, "roster_cache", None)
    records = cache.get_snapshot_records() if cache is not None else []
    stale_min = getattr(config, "disconnect_stale_minutes", 30)

    disconnected = []
    low_time = []
    for company, snap in records:
        if snap.connection.has_vehicle and (
            snap.connection.is_offline or snap.connection.is_stale(stale_min)
        ):
            stale_s = snap.connection.staleness_seconds()
            disconnected.append({
                "name": snap.name, "company": company,
                "vehicle": snap.connection.vehicle_number or "—",
                "last_seen": f"{int(stale_s // 60)}m ago" if stale_s is not None else "unknown",
                "duty_status": snap.duty_status_label,
            })

        drive_min = snap.hos.drive_seconds // 60
        shift_min = snap.hos.shift_seconds // 60
        break_min = snap.hos.break_seconds // 60
        worst = min(drive_min, shift_min, break_min)
        if worst <= 30:
            low_time.append({
                "name": snap.name, "company": company,
                "drive": drive_min, "shift": shift_min, "break": break_min,
                "violation": worst <= 0,
            })

    disconnected.sort(key=lambda r: r["name"])
    low_time.sort(key=lambda r: min(r["drive"], r["shift"], r["break"]))
    return render_template("watchlists.html", disconnected=disconnected, low_time=low_time)
