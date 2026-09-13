"""Statistics (upgrade spec §4.6) — aggregates send_log.jsonl by
day/week/month/all-time. Answers "do we have a violations problem" without
reading raw Telegram history."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template

from ... import send_log
from ..auth import login_required

bp = Blueprint("stats", __name__, url_prefix="/stats")


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    entries = send_log.read_all(getattr(runtime, "send_log_path", None))
    total_active = runtime.registry.count()
    buckets = send_log.aggregate(entries, datetime.now(timezone.utc), total_active)
    return render_template("stats.html", buckets=buckets, total_active=total_active)
