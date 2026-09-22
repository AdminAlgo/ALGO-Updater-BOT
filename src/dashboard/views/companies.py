"""Companies — every carrier the bot polls, with live counters.

Each row: USDOT, provider, how many drivers are low on time / disconnected /
without a group / new, and the company's status. ▸ opens the company's drivers
with their hours; ⋮ edits the company, its alert types and language, its API
key, tests the connection, or switches the whole company on/off.
"""

from __future__ import annotations

import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.eld import ELDError, build_provider

from ... import key_store
from .. import config_writer
from ..auth import login_required
from ..rows import company_rows, summarize
from ._common import ALERT_KINDS, LANGUAGES, group_options

bp = Blueprint("companies", __name__, url_prefix="/companies")


def _status(company) -> tuple[str, str]:
    """(label, chip class). load_config auto-disables an enabled company whose
    key is missing, so "no key" is told apart from a deliberate pause."""
    if company.enabled:
        return "Active", "chip-green"
    if company.company_key_env and not company.company_key:
        return "API key missing", "chip-red"
    return "Turned off", "chip-muted"


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
    rows = company_rows(runtime, config)
    by_company: dict[str, list] = {}
    for r in rows:
        by_company.setdefault(r["company"], []).append(r)
    for company_rows_ in by_company.values():
        company_rows_.sort(key=lambda r: (not r["low"], r["low_minutes"], r["name"].lower()))
    companies = []
    for c in config.companies:
        label, chip = _status(c)
        drivers = by_company.get(c.name, [])
        companies.append({"c": c, "status": label, "chip": chip,
                          "count": summarize(drivers), "drivers": drivers,
                          "off_kinds": ",".join(c.disabled_kinds)})
    totals = summarize(rows)
    return render_template(
        "companies.html", companies=companies, totals=totals,
        open_company=request.args.get("open", ""),
        alert_kinds=ALERT_KINDS, languages=LANGUAGES,
        groups=group_options(runtime.registry),
    )


@bp.post("/<name>/alerts")
@login_required
def alerts(name):
    """Company-wide alert types + default language."""
    runtime = current_app.config["RUNTIME"]
    enabled = set(request.form.getlist("kinds"))
    disabled = [k for k, _ in ALERT_KINDS if k not in enabled]
    language = request.form.get("language", "").strip() or None
    if language not in (None, *(c for c, _ in LANGUAGES)):
        language = None
    apply_all = request.form.get("apply_all") == "1"
    with runtime.lock:
        try:
            config_writer.set_company_fields(
                name, {"disabled_kinds": disabled, "language": language},
                runtime.config_path, runtime.env_path,
            )
        except Exception as exc:
            flash(f"Could not update {name}: {exc}", "error")
            return redirect(url_for("companies.index"))
        changed = 0
        if apply_all:
            ids = [r["driver_id"] for r in company_rows(runtime, runtime.current_config())
                   if r["company"] == name]
            # None clears each driver's own choice, so they follow the company.
            changed = runtime.registry.set_language_many(ids, None)
            runtime.registry.save()
    off = [label for code, label in ALERT_KINDS if code in disabled]
    msg = f"Saved {name}: " + (f"{', '.join(off)} switched OFF for the whole company"
                                if off else "every alert type is on")
    msg += f"; language {dict(LANGUAGES).get(language, 'English') if language else 'English (default)'}"
    if apply_all:
        msg += f" — applied to {changed} driver(s) that had their own language"
    flash(msg + ".", "ok")
    return redirect(url_for("companies.index"))


@bp.post("/<name>/test")
@login_required
def test_api(name):
    """One live roster call with this company's keys — proves a new API key works."""
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    company = next((c for c in config.companies if c.name == name), None)
    if company is None:
        flash(f"Company not found: {name}", "error")
    elif not company.company_key:
        flash(f"{name} has no API key yet — set one with ⋮ → Change API key.", "error")
    else:
        try:
            roster = build_provider(company, config.secrets).fetch_roster()
        except ELDError as exc:
            flash(f"{name}: API test FAILED — {exc}", "error")
        else:
            active = sum(1 for r in roster if r.get("active", True))
            flash(f"{name}: API connection OK — {active} active driver(s) on the roster.", "ok")
    return redirect(url_for("companies.index"))


@bp.post("/<name>/toggle")
@login_required
def toggle(name):
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    company = next((c for c in config.companies if c.name == name), None)
    if company is None:
        flash(f"Company not found: {name}", "error")
        return redirect(url_for("companies.index"))
    if not company.enabled and not company.company_key:
        # load_config would just switch it off again on the next read.
        flash(f"{name} can't be turned on — it has no API key. Use ⋮ → Change API key first.",
              "error")
        return redirect(url_for("companies.index"))
    with runtime.lock:
        try:
            config_writer.set_company_enabled(
                name, not company.enabled, runtime.config_path, runtime.env_path
            )
        except Exception as exc:
            flash(f"Could not update {name}: {exc}", "error")
        else:
            flash(f"{name} is now {'OFF — no polling, no updates' if company.enabled else 'ON'}.",
                  "ok")
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
