"""Admin login for the dashboard — username + password.

One operator account, set by environment variables:
    DASHBOARD_ADMIN_USERNAME   (optional, default "admin")
    DASHBOARD_ADMIN_PASSWORD   (required)

The password is hashed once at first check (not at import time, so importing
this module never requires DASHBOARD_ADMIN_PASSWORD to already be set) and
compared with a constant-time check. Repeated failures from one address are
locked out for a while, since the panel is on a public URL.
"""

from __future__ import annotations

import functools
import hmac
import logging
import os
import threading
import time

from flask import redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("eld_alert_bot.dashboard")

_password_hash: str | None = None

#: failed attempts allowed per address inside LOCKOUT_WINDOW before refusing
MAX_FAILURES = 10
LOCKOUT_WINDOW = 10 * 60
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def admin_username() -> str:
    return (os.environ.get("DASHBOARD_ADMIN_USERNAME") or "").strip() or "admin"


def _get_password_hash() -> str:
    global _password_hash
    if _password_hash is None:
        _password_hash = generate_password_hash(os.environ["DASHBOARD_ADMIN_PASSWORD"])
    return _password_hash


def check_password(password: str) -> bool:
    return check_password_hash(_get_password_hash(), password)


def check_credentials(username: str, password: str) -> bool:
    """Both must match. The password is always checked, so a wrong username
    takes as long as a wrong password and reveals nothing."""
    user_ok = hmac.compare_digest((username or "").strip().lower().encode(),
                                  admin_username().lower().encode())
    pass_ok = check_password(password or "")
    return user_ok and pass_ok


def locked_out(address: str) -> bool:
    now = time.monotonic()
    with _failures_lock:
        recent = [t for t in _failures.get(address, []) if now - t < LOCKOUT_WINDOW]
        _failures[address] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(address: str) -> None:
    with _failures_lock:
        _failures.setdefault(address, []).append(time.monotonic())


def clear_failures(address: str) -> None:
    with _failures_lock:
        _failures.pop(address, None)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped
