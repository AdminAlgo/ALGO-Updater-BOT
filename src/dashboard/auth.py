"""Single-shared-admin password login for the dashboard.

Matches "that admin will control everything" — one operator, no per-user
accounts or roles. Password is hashed once at first check (not at import
time, so importing this module never requires DASHBOARD_ADMIN_PASSWORD to
already be set) and compared with a constant-time check.
"""

from __future__ import annotations

import functools
import logging
import os

from flask import redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("eld_alert_bot.dashboard")

_password_hash: str | None = None


def _get_password_hash() -> str:
    global _password_hash
    if _password_hash is None:
        _password_hash = generate_password_hash(os.environ["DASHBOARD_ADMIN_PASSWORD"])
    return _password_hash


def check_password(password: str) -> bool:
    return check_password_hash(_get_password_hash(), password)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped
