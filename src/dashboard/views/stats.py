"""Statistics — how many drivers actually got updates (per day / week / month /
all time, in numbers and %), which alert types went out, and whether
violations are a problem, overall and per company. Aggregated from
send_log.jsonl."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template, request

from ... import send_log
from ...messages import KIND_LABELS
from ..auth import login_required
from ..rows import company_rows

bp = Blueprint("stats", __name__, url_prefix="/stats")

BUCKETS = (("day", "Last 24 hours"), ("week", "Last 7 days"),
           ("month", "Last 30 days"), ("all_time", "All time"))


def kind_label(kind: str) -> str:
    if kind.startswith("broadcast:"):
        return "Message: " + kind.split(":", 1)[1]
    return KIND_LABELS.get(kind, kind)


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    now = datetime.now(timezone.utc)
    entries = send_log.read_all(getattr(runtime, "send_log_path", None))
    rows = company_rows(runtime, config, now)
    monitored = len(rows) or runtime.registry.count()
    linked = sum(1 for r in rows if r["linked"]) if rows else runtime.registry.count()
    buckets = send_log.aggregate(entries, now, monitored, linked)
    selected = request.args.get("period", "week")
    if selected not in dict(BUCKETS):
        selected = "week"
    return render_template(
        "stats.html", buckets=buckets, periods=BUCKETS, selected=selected,
        b=buckets[selected], monitored=monitored, linked=linked,
        series=send_log.daily(entries, now, 30), kind_label=kind_label,
        first_entry=(send_log.parse_ts(entries[0]) if entries else None),
    )
