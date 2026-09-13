from __future__ import annotations

import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from .. import config_writer
from ..auth import login_required

bp = Blueprint("companies", __name__, url_prefix="/companies")


def _valid_key_env(name: str) -> bool:
    """A company_key_env must be a usable shell/env identifier.

    Worth enforcing here: a name with a space in it (`MILEMAX LLC`) can be set
    on Railway without complaint but can never be read back by the loader, so
    the company silently disables itself every cycle.
    """
    return bool(name) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None


def _counters(runtime, config):
    """Per-company low-time / disconnected counters (§4.3), from the same
    RosterCache snapshot Watchlists uses — no extra live fetch."""
    cache = getattr(runtime, "roster_cache", None)
    records = cache.get_snapshot_records() if cache is not None else []
    stale_min = getattr(config, "disconnect_stale_minutes", 30)
    by_company: dict[str, dict] = {}
    for company_name, snap in records:
        c = by_company.setdefault(company_name, {"total": 0, "low_time": 0, "disconnected": 0})
        c["total"] += 1
        worst_min = min(snap.hos.drive_seconds, snap.hos.shift_seconds, snap.hos.break_seconds) // 60
        if worst_min <= 30:
            c["low_time"] += 1
        if snap.connection.has_vehicle and (
            snap.connection.is_offline or snap.connection.is_stale(stale_min)
        ):
            c["disconnected"] += 1
    return by_company


def _drivers_for_company(runtime, company_name: str):
    """Registered driver rows for one company's expand-row (§4.3), matched via
    the roster cache's driver_id -> company map."""
    cache = getattr(runtime, "roster_cache", None)
    driver_ids = {c.driver_id for c in (cache.get() if cache is not None else [])
                 if c.company == company_name}
    records = runtime.registry.all()
    return [{"driver_id": did, **rec} for did, rec in records.items() if did in driver_ids]


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    counters = _counters(runtime, config)
    expanded = {}
    for c in config.companies:
        expanded[c.name] = _drivers_for_company(runtime, c.name)
    return render_template(
        "companies.html", companies=config.companies, counters=counters,
        expanded=expanded,
    )


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
    usdot = request.form.get("usdot", "").strip()
    mc_number = request.form.get("mc_number", "").strip()
    key_env = request.form.get("company_key_env", "").strip()
    if not name or provider not in ("factor", "leader") or not chat_id:
        flash("Name, provider (factor/leader), and chat id are required.", "error")
        return redirect(url_for("companies.index"))
    if not _valid_key_env(key_env):
        flash("API key env var name is required, and may contain only letters, "
              "digits and underscores (no spaces).", "error")
        return redirect(url_for("companies.index"))
    company = {
        "name": name,
        "provider": provider,
        "driver_group_chat_id": chat_id,
        "enabled": False,  # always added paused — enable only after --eld-check passes
        "monitor_all_drivers": True,
        "usdot": usdot,
        "mc_number": mc_number,
        "company_key_env": key_env,
    }
    with runtime.lock:
        try:
            config_writer.add_company(company, runtime.config_path, runtime.env_path)
        except Exception as exc:
            flash(f"Could not add company: {exc}", "error")
        else:
            flash(f"Added {name}, disabled by default — enable once its API key is verified "
                  f"(python main.py --eld-check).", "ok")
    return redirect(url_for("companies.index"))


@bp.post("/<name>/key-env")
@login_required
def key_env(name):
    """Point a company at the env var holding its API key (§4.3 / §7 option c).

    The secret itself still lives only in Railway/.env — this stores the *name*
    so the loader can find it. Without this a company added through the panel
    has no company_key_env at all and can never be enabled.
    """
    runtime = current_app.config["RUNTIME"]
    value = request.form.get("company_key_env", "").strip()
    if value and not _valid_key_env(value):
        flash("Env var name may contain only letters, digits and underscores "
              "(no spaces).", "error")
        return redirect(url_for("companies.index"))
    with runtime.lock:
        try:
            # Pass "" rather than None to clear: update_company filters None out,
            # and load_config treats an empty string as "not configured" (it only
            # rejects that for a company that is still enabled).
            config_writer.update_company(
                name, {"company_key_env": value},
                runtime.config_path, runtime.env_path,
            )
        except Exception as exc:
            flash(f"Could not update {name}: {exc}", "error")
        else:
            if value:
                flash(f"{name} now reads its API key from {value}. Set that "
                      f"variable on Railway, then redeploy and enable it.", "ok")
            else:
                flash(f"Cleared the API key variable name for {name}.", "ok")
    return redirect(url_for("companies.index"))


@bp.post("/<name>/edit")
@login_required
def edit(name):
    """Edit display fields (§4.3): name, USDOT, MC#, low-hours closing line.
    Provider / company_key_env / enabled have their own dedicated flows."""
    runtime = current_app.config["RUNTIME"]
    new_name = request.form.get("name", "").strip() or name
    updates = {
        "name": new_name,
        "usdot": request.form.get("usdot", "").strip(),
        "mc_number": request.form.get("mc_number", "").strip(),
        "low_hours_closing": request.form.get("low_hours_closing", "").strip(),
    }
    with runtime.lock:
        try:
            config_writer.update_company(name, updates, runtime.config_path, runtime.env_path)
        except Exception as exc:
            flash(f"Could not update {name}: {exc}", "error")
        else:
            flash(f"Saved {new_name}.", "ok")
    return redirect(url_for("companies.index"))
