"""Overview — the landing page: what needs attention right now, whether the
bot is healthy, and what it has sent recently."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, render_template

from ... import send_log
from ..auth import login_required
from ..rows import company_rows, summarize

bp = Blueprint("status", __name__)


def _today_counts(runtime, now: datetime) -> dict:
    since = now - timedelta(hours=24)
    sent = violations = failed = 0
    for e in send_log.read_all(getattr(runtime, "send_log_path", None)):
        ts = send_log.parse_ts(e)
        if ts is None or ts < since:
            continue
        if e.get("ok"):
            sent += 1
            if e.get("kind") == "shift_violation" and not e.get("resend"):
                violations += 1
        elif not e.get("suppressed"):
            failed += 1
    return {"sent": sent, "violations": violations, "failed": failed}


@bp.get("/")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    now = datetime.now(timezone.utc)
    rows = company_rows(runtime, config, now)
    counts = summarize(rows)
    checkin_min = min(config.off_duty_checkin_days) if config.off_duty_checkin_days else 2
    counts["resting"] = sum(1 for r in rows if (r["rest_days"] or 0) >= checkin_min)
    return render_template(
        "status.html",
        counts=counts,
        today=_today_counts(runtime, now),
        checkin_min=checkin_min,
        low_minutes=config.watchlist_low_minutes,
        companies_on=sum(1 for c in config.companies if c.enabled),
        companies_total=len(config.companies),
        stats=runtime.last_cycle_stats,
        last_cycle_at=runtime.last_cycle_at,
        coverage=runtime.registry.coverage(),
        recent=runtime.activity_log.recent(50),
        admin_configured=bool(config.admin_user_ids),
    )
