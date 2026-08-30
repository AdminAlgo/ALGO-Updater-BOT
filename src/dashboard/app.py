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


def create_app(runtime: RuntimeContext) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ["DASHBOARD_SECRET_KEY"]
    app.config["RUNTIME"] = runtime
    app.config["PERMANENT_SESSION_LIFETIME"] = SEVEN_DAYS_SECONDS

    from .views.auth import bp as auth_bp
    from .views.companies import bp as companies_bp
    from .views.drivers import bp as drivers_bp
    from .views.health import bp as health_bp
    from .views.status import bp as status_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(companies_bp)
    app.register_blueprint(drivers_bp)
    app.register_blueprint(health_bp)

    return app
