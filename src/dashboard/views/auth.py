from __future__ import annotations

import logging

from flask import Blueprint, redirect, render_template, request, session, url_for

from ..auth import check_password

log = logging.getLogger("eld_alert_bot.dashboard")

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password(password):
            session.clear()
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("status.index"))
        log.warning("dashboard: failed login attempt")
        error = "Incorrect password."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
