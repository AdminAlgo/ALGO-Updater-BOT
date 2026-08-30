"""Unauthenticated health check — for Railway's healthcheckPath."""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    return {"ok": True}
