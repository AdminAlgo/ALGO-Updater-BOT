"""Flask app factory for the admin dashboard.

Server-rendered HTML (Jinja2, plain forms), no JS framework — matches this
codebase's existing low-dependency style, and a single-admin, low-traffic
internal tool gains nothing from a JS/SPA layer.
"""

from __future__ import annotations

import os

from flask import Flask

from .runtime import RuntimeContext

SEVEN_DAYS_SECONDS = 7 * 24 * 3600


def environment_name() -> str:
    """"staging" or "production" — drives the colour scheme and the header badge.

    Two deploys of this panel look identical otherwise, and one of them can page
    real drivers, so it must be obvious at a glance which one is open. Falls back
    to Railway's own RAILWAY_ENVIRONMENT_NAME, so nothing extra has to be set.
    """
    name = (os.environ.get("APP_ENV")
            or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
            or "").strip().lower()
    return "staging" if name.startswith(("stag", "test", "dev")) else "production"


def create_app(runtime: RuntimeContext) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ["DASHBOARD_SECRET_KEY"]
    app.config["RUNTIME"] = runtime
    app.config["PERMANENT_SESSION_LIFETIME"] = SEVEN_DAYS_SECONDS

    @app.context_processor
    def _inject_env():
        return {"app_env": environment_name()}

    from .views.auth import bp as auth_bp
    from .views.companies import bp as companies_bp
    from .views.drivers import bp as drivers_bp
    from .views.groups import bp as groups_bp
    from .views.health import bp as health_bp
    from .views.stats import bp as stats_bp
    from .views.status import bp as status_bp
    from .views.warnings import bp as warnings_bp
    from .views.watchlists import bp as watchlists_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(watchlists_bp)
    app.register_blueprint(companies_bp)
    app.register_blueprint(drivers_bp)
    app.register_blueprint(groups_bp)
    app.register_blueprint(warnings_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(health_bp)

    return app
