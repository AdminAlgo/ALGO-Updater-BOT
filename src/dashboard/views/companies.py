from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from .. import config_writer
from ..auth import login_required

bp = Blueprint("companies", __name__, url_prefix="/companies")


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    return render_template("companies.html", companies=config.companies)


@bp.post("/<name>/toggle")
@login_required
def toggle(name):
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    company = next((c for c in config.companies if c.name == name), None)
    if company is None:
        flash(f"Company not found: {name}", "error")
        return redirect(url_for("companies.index"))
    with runtime.lock:
        try:
            config_writer.set_company_enabled(
                name, not company.enabled, runtime.config_path, runtime.env_path
            )
        except Exception as exc:
            flash(f"Could not update {name}: {exc}", "error")
        else:
            flash(f"{name} is now {'disabled' if company.enabled else 'enabled'}.", "ok")
    return redirect(url_for("companies.index"))


@bp.post("/new")
@login_required
def new():
    runtime = current_app.config["RUNTIME"]
    name = request.form.get("name", "").strip()
    provider = request.form.get("provider", "").strip()
    chat_id = request.form.get("driver_group_chat_id", "").strip()
    if not name or provider not in ("factor", "leader") or not chat_id:
        flash("Name, provider (factor/leader), and chat id are required.", "error")
        return redirect(url_for("companies.index"))
    company = {
        "name": name,
        "provider": provider,
        "driver_group_chat_id": chat_id,
        "enabled": False,  # always added paused — enable only after --eld-check passes
        "monitor_all_drivers": True,
    }
    with runtime.lock:
        try:
            config_writer.add_company(company, runtime.config_path, runtime.env_path)
        except Exception as exc:
            flash(f"Could not add company: {exc}", "error")
        else:
            flash(f"Added {name}, disabled by default — enable once its bearer session is verified.", "ok")
    return redirect(url_for("companies.index"))
