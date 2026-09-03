from __future__ import annotations

from flask import Blueprint, current_app, render_template

from ..auth import login_required

bp = Blueprint("status", __name__)


@bp.get("/")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    return render_template(
        "status.html",
        stats=runtime.last_cycle_stats,
        last_cycle_at=runtime.last_cycle_at,
        coverage=runtime.registry.coverage(),
        recent=runtime.activity_log.recent(50),
        admin_configured=bool(config.admin_user_ids),
    )
