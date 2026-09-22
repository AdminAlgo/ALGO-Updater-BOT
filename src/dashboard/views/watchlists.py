"""Watchlists — the drivers that need someone to act now.

Read-only lenses over the live RosterCache snapshot the scheduler primes every
cycle (no extra ELD fetch), using the same row logic as the Drivers page:

  * Rolling while disconnected — Driving with the ELD offline/stale.
  * Disconnected now           — active drivers (all drivers with ?all=1).
  * Low time                   — any clock at/under N minutes (30/60/120).
  * Off duty for days          — resting 1+ day; the vacation check-in list.

Each list downloads as CSV, so it can be pasted into a dispatch chat.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, render_template, request

from ..auth import login_required
from ..rows import company_rows
from ._common import ALERT_KINDS, LANGUAGES, csv_response, group_options

bp = Blueprint("watchlists", __name__, url_prefix="/watchlists")

LOW_CHOICES = (30, 60, 120)


def _lists(runtime, config):
    rows = company_rows(runtime, config)
    include_all = request.args.get("all") == "1"
    try:
        low_min = int(request.args.get("min", config.watchlist_low_minutes))
    except ValueError:
        low_min = config.watchlist_low_minutes
    low_min = max(1, min(low_min, 14 * 60))

    rolling = [r for r in rows if r["rolling"]]
    disconnected = [r for r in rows if r["disconnected"] and (include_all or r["active"])]
    # rows' own "low" flag uses the configured minutes; this list lets the
    # operator widen it (60/120) without touching the setting.
    low = [r for r in rows if not r["no_shift"] and r["low_seconds"] <= low_min * 60]
    resting = [r for r in rows if r["rest_days"] is not None and r["rest_days"] >= 1]

    rolling.sort(key=lambda r: r["name"].lower())
    disconnected.sort(key=lambda r: (not r["rolling"], r["name"].lower()))
    low.sort(key=lambda r: (not r["active"], r["low_minutes"]))
    resting.sort(key=lambda r: -r["rest_days"])
    return {"rolling": rolling, "disconnected": disconnected, "low": low,
            "resting": resting, "include_all": include_all, "low_min": low_min}


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    lists = _lists(runtime, config)
    return render_template(
        "watchlists.html", **lists, low_choices=LOW_CHOICES,
        checkin_days=config.off_duty_checkin_days,
        alert_kinds=ALERT_KINDS, languages=LANGUAGES, groups=group_options(runtime.registry),
    )


@bp.get("/<name>.csv")
@login_required
def export(name):
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    lists = _lists(runtime, config)
    if name not in ("rolling", "disconnected", "low", "resting"):
        abort(404)
    rows = lists[name]
    if name == "resting":
        return csv_response("off-duty-drivers.csv",
                            ["Driver", "Company", "Status", "Days off", "Truck", "Location",
                             "Group"],
                            [[r["name"], r["company"], r["status_label"],
                              f"{r['rest_days']:.1f}", r["truck"], r["location"],
                              r["eld_title"] or "NO GROUP"] for r in rows])
    return csv_response(f"{name}-drivers.csv",
                        ["Driver", "Company", "Status", "Truck", "Location", "Last seen",
                         "Break", "Drive", "Shift", "Cycle", "Lowest clock", "Group"],
                        [[r["name"], r["company"], r["status_label"], r["truck"],
                          r["location"], r["last_seen"], *[t["text"] for t in r["timers"]],
                          f"{r['low_clock']} {r['low_minutes']}m", r["eld_title"] or "NO GROUP"]
                         for r in rows])

