"""Settings — the global alert rules, editable without touching config.yaml
by hand: vacation check-in days, low-hours thresholds, cycle thresholds,
disconnect alerts, the long On Duty check, and the watchlist's low-time line.

Writes go through config_writer (validated with a real load_config first), so
a bad value can never stop the bot.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from .. import config_writer
from ..auth import login_required

bp = Blueprint("settings", __name__, url_prefix="/settings")


def _ints(text: str, label: str, problems: list) -> list[int]:
    out = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            problems.append(f"{label}: “{part}” is not a whole number")
            continue
        if n <= 0:
            problems.append(f"{label}: numbers must be above 0")
            continue
        out.append(n)
    return sorted(set(out), reverse=True)


def _int(text: str, label: str, problems: list, default: int) -> int:
    try:
        n = int(str(text).strip())
        if n > 0:
            return n
    except ValueError:
        pass
    problems.append(f"{label} must be a whole number above 0")
    return default


def _durable(config_path: str) -> bool | None:
    """Is config.yaml on a mounted volume? None = local run (can't tell)."""
    path = Path(config_path).resolve()
    if not os.path.isabs(os.environ.get("DATA_DIR", ".")):
        return None
    for parent in [path.parent, *path.parents]:
        try:
            if os.path.ismount(parent) and str(parent) not in ("/", path.anchor):
                return True
        except OSError:
            break
    return False


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    return render_template("settings.html", c=config, durable=_durable(runtime.config_path),
                           config_path=runtime.config_path)


@bp.post("")
@login_required
def save():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    f = request.form
    problems: list[str] = []
    fields = {
        "off_duty_checkin_days": sorted(_ints(f.get("off_duty_checkin_days", ""),
                                              "Vacation check-in days", problems)),
        "low_hours_thresholds_minutes.driver_group": _ints(
            f.get("low_hours", ""), "Low-hours warnings", problems),
        "cycle_alert_thresholds_hours": _ints(f.get("cycle", ""), "Cycle warnings", problems),
        "on_duty_alert_hours": _int(f.get("on_duty_alert_hours", ""), "Long On Duty hours",
                                    problems, int(config.on_duty_alert_hours or 2)),
        "disconnect_alerts_enabled": f.get("disconnect_alerts_enabled") == "1",
        "disconnect_stale_minutes": _int(f.get("disconnect_stale_minutes", ""),
                                         "Disconnected after", problems,
                                         config.disconnect_stale_minutes),
        "disconnect_realert_minutes": _int(f.get("disconnect_realert_minutes", ""),
                                           "Repeat disconnect alert every", problems,
                                           config.disconnect_realert_minutes),
        "shift_violation_resend_minutes": _int(f.get("shift_violation_resend_minutes", ""),
                                               "Repeat violation every", problems,
                                               config.shift_violation_resend_minutes),
        "watchlist_low_minutes": _int(f.get("watchlist_low_minutes", ""),
                                      "Low time line", problems, config.watchlist_low_minutes),
    }
    if f.get("on_duty_off") == "1":
        fields["on_duty_alert_hours"] = 0
    if problems:
        flash("Not saved — " + "; ".join(problems), "error")
        return redirect(url_for("settings.index"))
    with runtime.lock:
        try:
            config_writer.set_settings(fields, runtime.config_path, runtime.env_path)
        except Exception as exc:
            flash(f"Not saved — {exc}", "error")
            return redirect(url_for("settings.index"))
    flash("Settings saved — they apply from the next poll cycle (within 2 minutes).", "ok")
    return redirect(url_for("settings.index"))
