from __future__ import annotations

import logging

from flask import Blueprint, redirect, render_template, request, session, url_for

from ..auth import (admin_username, check_credentials, clear_failures, locked_out,
                    record_failure)

log = logging.getLogger("eld_alert_bot.dashboard")

bp = Blueprint("auth", __name__)


def _client_address() -> str:
    # Railway puts the real client first in X-Forwarded-For.
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or request.remote_addr or "?"


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    username = ""
    if request.method == "POST":
        address = _client_address()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if locked_out(address):
            log.warning("dashboard: login refused — too many failed attempts from %s", address)
            error = "Too many failed attempts. Wait 10 minutes and try again."
        elif check_credentials(username, password):
            clear_failures(address)
            session.clear()
            session["authed"] = True
            session["user"] = admin_username()
            session.permanent = True
            return redirect(url_for("status.index"))
        else:
            record_failure(address)
            log.warning("dashboard: failed login attempt for user %r", username[:40])
            error = "Wrong username or password."
    return render_template("login.html", error=error, username=username)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
