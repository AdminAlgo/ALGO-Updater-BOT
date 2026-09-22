from __future__ import annotations

import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ... import key_store
from .. import config_writer
from ..auth import login_required

bp = Blueprint("companies", __name__, url_prefix="/companies")


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


def _key_already_used(config, api_key: str, *, except_name: str | None = None):
    """The company already authenticating with this key, if any.

    One API key is one ELD account. Giving it to a second company doesn't add a
    carrier — it re-adds the same drivers under another name, and every one of
    them then shows up twice in /roster, /assign and the Drivers page.
    """
    return next(
        (c for c in config.companies
         if c.company_key and c.company_key == api_key and c.name != except_name),
        None,
    )


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
    usdot = request.form.get("usdot", "").strip()
    api_key = request.form.get("api_key", "").strip()
    if not name or provider not in ("factor", "leader"):
        flash("Name and provider (factor/leader) are required.", "error")
        return redirect(url_for("companies.index"))
    if not api_key:
        flash("API key is required.", "error")
        return redirect(url_for("companies.index"))

    config = runtime.current_config()
    if any(c.name.strip().lower() == name.lower() for c in config.companies):
        flash(f"A company named {name} already exists.", "error")
        return redirect(url_for("companies.index"))
    clash = _key_already_used(config, api_key)
    if clash is not None:
        flash(f"That API key already belongs to {clash.name} — one key is one "
              f"ELD account, so adding it again would list the same drivers "
              f"twice and stop them being assigned to a group. If the carrier "
              f"changed its name, rename {clash.name} instead (⋮ → Edit).",
              "error")
        return redirect(url_for("companies.index"))

    try:
        key_env = key_store.env_name_for(name)
    except key_store.KeyStoreError as exc:
        flash(f"Could not derive a key name from {name!r}: {exc}", "error")
        return redirect(url_for("companies.index"))

    company = {
        "name": name,
        "provider": provider,
        # Per-driver groups come from the registry; this company-wide fallback is
        # only used when a driver has no group of their own, so it inherits the
        # central chat rather than asking for a chat id the operator doesn't have
        # yet. config.yaml requires it to be a non-empty string.
        "driver_group_chat_id": config.team_group_chat_id,
        "enabled": False,  # always added paused — enable once the roster looks right
        "monitor_all_drivers": True,
        "usdot": usdot,
        "mc_number": "",
        "company_key_env": key_env,
    }
    with runtime.lock:
        # Store the key first: if this fails the company is never written, which
        # is better than a company that exists but can never authenticate.
        try:
            key_store.put(key_env, api_key)
        except Exception as exc:
            flash(f"Could not store the API key: {exc}", "error")
            return redirect(url_for("companies.index"))
        try:
            config_writer.add_company(company, runtime.config_path, runtime.env_path)
        except Exception as exc:
            key_store.delete(key_env)  # don't leave an orphaned secret behind
            flash(f"Could not add company: {exc}", "error")
        else:
            flash(f"Added {name}, paused. Its API key is stored — open the ⋮ menu "
                  f"and Enable it once the driver count looks right.", "ok")
    return redirect(url_for("companies.index"))


@bp.post("/<name>/key-env")
@login_required
def key_env(name):
    """Replace a company's API key (§4.3).

    The key is written to the encrypted store, not into config.yaml. A company
    whose key currently comes from a host environment variable keeps reading
    that variable — the host wins in load_config — so this reports that case
    instead of saving a value that would be silently ignored.
    """
    runtime = current_app.config["RUNTIME"]
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        flash("API key is required.", "error")
        return redirect(url_for("companies.index"))

    config = runtime.current_config()
    company = next((c for c in config.companies if c.name == name), None)
    if company is None:
        flash(f"Company not found: {name}", "error")
        return redirect(url_for("companies.index"))

    clash = _key_already_used(config, api_key, except_name=company.name)
    if clash is not None:
        flash(f"That API key already belongs to {clash.name} — one key is one "
              f"ELD account, and sharing it would list every driver on it "
              f"twice. Use {name}'s own key.", "error")
        return redirect(url_for("companies.index"))

    key_env_name = company.company_key_env
    if not key_env_name:
        try:
            key_env_name = key_store.env_name_for(name)
        except key_store.KeyStoreError as exc:
            flash(f"Could not derive a key name for {name}: {exc}", "error")
            return redirect(url_for("companies.index"))

    with runtime.lock:
        try:
            key_store.put(key_env_name, api_key)
            if company.company_key_env != key_env_name:
                config_writer.update_company(
                    name, {"company_key_env": key_env_name},
                    runtime.config_path, runtime.env_path,
                )
        except Exception as exc:
            flash(f"Could not update {name}: {exc}", "error")
            return redirect(url_for("companies.index"))

    if os.environ.get(key_env_name, "").strip():
        flash(f"Saved, but {name} still reads {key_env_name} from the host "
              f"environment, which takes priority. Remove that variable on "
              f"Railway for the new key to take effect.", "error")
    else:
        flash(f"Updated the API key for {name}. It applies on the next poll "
              f"cycle — no redeploy needed.", "ok")
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
