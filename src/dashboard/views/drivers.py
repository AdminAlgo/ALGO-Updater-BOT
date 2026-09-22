"""Drivers — every driver on the live roster, laid out like the ELD platform.

One row per driver: duty status, truck + connection, location, the four HOS
clocks, and whether updates reach them. The ⋮ menu edits the driver's ELD and
Dispatch groups (by typing the group's name), language, which alert types
they get, or switches their updates off entirely.
"""

from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.eld import ELDError, build_provider

from ..auth import login_required
from ..rows import company_rows
from ._common import (ALERT_KINDS, LANGUAGES, csv_response, group_options, resolve_group,
                      safe_next)

bp = Blueprint("drivers", __name__, url_prefix="/drivers")

FILTERS = [
    ("all", "All"),
    ("unlinked", "No group"),
    ("new", "New"),
    ("low", "Low time"),
    ("disconnected", "Disconnected"),
    ("active", "On duty / driving"),
    ("paused", "Updates off"),
]

_FILTER_FNS = {
    "all": lambda r: True,
    "unlinked": lambda r: not r["linked"],
    "new": lambda r: r["is_new"],
    "low": lambda r: r["low"],
    "disconnected": lambda r: r["disconnected"],
    "active": lambda r: r["active"],
    "paused": lambda r: r["paused"],
}

SORTS = {
    "name": lambda r: r["name"].lower(),
    "time": lambda r: (not r["low"], r["low_minutes"], r["name"].lower()),
    "status": lambda r: (r["status_short"], r["name"].lower()),
    "company": lambda r: (r["company"].lower(), r["name"].lower()),
}


def _live_candidates(runtime):
    """Roster (Candidate rows) for pickers — memory-served from RosterCache.

    Opening a page must NOT fire its own DriveHOS fetch: the provider key is
    shared and uncoordinated fetches trip the rate limit. Falls back to a
    direct fetch only when no cache is wired (tests, CLI tools).
    """
    cache = getattr(runtime, "roster_cache", None)
    if cache is not None:
        cands = cache.get()
        errors = [cache.last_error] if cache.last_error else []
        return cands, errors

    config = runtime.current_config()
    candidates: list = []
    errors: list[str] = []
    for company in config.companies:
        if not company.enabled:
            continue
        try:
            provider = build_provider(company, config.secrets)
            result = provider.fetch_snapshots(
                company.drivers, all_active=company.monitor_all_drivers
            )
        except ELDError as exc:
            errors.append(f"{company.name}: {exc}")
            continue
        from src.registry import Candidate
        for snap in result.snapshots:
            candidates.append(Candidate(snap.driver_id, snap.name, snap.username,
                                        snap.connection.vehicle_number, company.name))
    return candidates, errors


def _filtered_rows(runtime, config):
    rows = company_rows(runtime, config)
    company = request.args.get("company", "").strip()
    flt = request.args.get("filter", "all")
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort", "name")
    counts = {key: sum(1 for r in rows if (not company or r["company"] == company)
                       and _FILTER_FNS[key](r)) for key, _ in FILTERS}
    if company:
        rows = [r for r in rows if r["company"] == company]
    rows = [r for r in rows if _FILTER_FNS.get(flt, _FILTER_FNS["all"])(r)]
    if q:
        rows = [r for r in rows if q in f"{r['name']} {r['truck']} {r['eld_title']}".lower()]
    rows.sort(key=SORTS.get(sort, SORTS["name"]))
    return rows, counts, company, flt, q, sort


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    rows, counts, company, flt, q, sort = _filtered_rows(runtime, config)
    on_roster = {r["driver_id"] for r in company_rows(runtime, config)}
    off_roster = sorted(
        ({"driver_id": did, **rec} for did, rec in runtime.registry.all().items()
         if did not in on_roster and rec.get("chat_id")),
        key=lambda rec: (rec.get("driver_name") or "").lower(),
    )
    cache = getattr(runtime, "roster_cache", None)
    return render_template(
        "drivers.html",
        rows=rows, counts=counts, filters=FILTERS, company_filter=company,
        active_filter=flt, query=q, sort=sort,
        companies=[c.name for c in config.companies if c.enabled],
        off_roster=off_roster,
        roster_errors=[cache.last_error] if cache is not None and cache.last_error else [],
        alert_kinds=ALERT_KINDS, languages=LANGUAGES,
        groups=group_options(runtime.registry),
        low_minutes=config.watchlist_low_minutes,
    )


@bp.get("/export.csv")
@login_required
def export():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    rows, *_ = _filtered_rows(runtime, config)
    return csv_response("drivers.csv", [
        "Driver", "Company", "Status", "Truck", "Location", "Last seen", "Break", "Drive",
        "Shift", "Cycle", "Disconnected", "ELD group", "Dispatch group", "Updates", "Language",
    ], [[
        r["name"], r["company"], r["status_label"], r["truck"], r["location"], r["last_seen"],
        *[t["text"] for t in r["timers"]], "yes" if r["disconnected"] else "",
        r["eld_title"], r["dispatch_title"],
        "no group" if not r["linked"] else ("off" if r["paused"] else "on"), r["language"],
    ] for r in rows])


@bp.post("/<driver_id>/edit")
@login_required
def edit(driver_id):
    """Save the driver dialog: ELD + Dispatch group, language, alert types,
    updates on/off. Also how a driver with no group gets one."""
    runtime = current_app.config["RUNTIME"]
    registry = runtime.registry
    back = safe_next("drivers.index")
    name = request.form.get("driver_name", "").strip()

    chat_id, err = resolve_group(registry, request.form.get("chat_id", ""))
    if err or not chat_id:
        flash(err or "Choose the driver's ELD group.", "error")
        return redirect(back)
    dispatch_id, err = resolve_group(registry, request.form.get("dispatch_chat_id", ""))
    if err:
        flash(f"Dispatch group: {err}", "error")
        return redirect(back)
    if dispatch_id and dispatch_id == chat_id:
        flash("The Dispatch group must be a different group from the ELD group.", "error")
        return redirect(back)

    language = request.form.get("language", "").strip() or None
    if language not in (None, *(c for c, _ in LANGUAGES)):
        language = None
    enabled = set(request.form.getlist("kinds"))
    disabled_kinds = [k for k, _ in ALERT_KINDS if k not in enabled]
    updates_on = request.form.get("updates_on") == "1"

    with runtime.lock:
        registry.unblock(driver_id)
        registry.update_driver_settings(
            driver_id, driver_name=name or None, chat_id=chat_id,
            title=registry.group_title(chat_id) or "",
            dispatch_chat_id=dispatch_id, language=language,
            disabled_kinds=disabled_kinds, paused=not updates_on,
        )
        registry.clear_pending(chat_id)
        registry.save()
    title = registry.group_title(chat_id) or chat_id
    note = "" if updates_on else " Updates are OFF for this driver."
    flash(f"Saved {name or driver_id} → {title}.{note}", "ok")
    return redirect(back)


@bp.post("/<driver_id>/pause")
@login_required
def pause(driver_id):
    runtime = current_app.config["RUNTIME"]
    paused = request.form.get("paused") == "1"
    name = runtime.registry.driver_name(driver_id) or driver_id
    with runtime.lock:
        ok = runtime.registry.set_paused(driver_id, paused)
        runtime.registry.save()
    if not ok:
        flash(f"{name} has no group yet — assign one first.", "error")
    elif paused:
        flash(f"Updates are OFF for {name}. Nothing will be sent until you turn them back on.", "ok")
    else:
        flash(f"Updates are ON again for {name}.", "ok")
    return redirect(safe_next("drivers.index"))


@bp.post("/<driver_id>/unregister")
@login_required
def unregister(driver_id):
    runtime = current_app.config["RUNTIME"]
    name = runtime.registry.driver_name(driver_id) or driver_id
    with runtime.lock:
        runtime.registry.unregister(driver_id)
        runtime.registry.block(driver_id)  # stays out even if title auto-match would re-add
        runtime.registry.save()
    flash(f"Unlinked {name} — no more alerts until a group is assigned again.", "ok")
    return redirect(safe_next("drivers.index"))


@bp.post("/<driver_id>/unblock")
@login_required
def unblock(driver_id):
    runtime = current_app.config["RUNTIME"]
    with runtime.lock:
        runtime.registry.unblock(driver_id)
        runtime.registry.save()
    flash("Auto-linking can pick this driver up again.", "ok")
    return redirect(safe_next("drivers.index"))


@bp.post("/assign")
@login_required
def assign():
    """Legacy one-field assign (chat id or group name) — kept for old links."""
    runtime = current_app.config["RUNTIME"]
    driver_id = request.form.get("driver_id", "").strip()
    name = request.form.get("driver_name", "").strip()
    chat_id, err = resolve_group(runtime.registry, request.form.get("chat_id", ""))
    if not driver_id or not name or not chat_id:
        flash(err or "Driver and group are required.", "error")
        return redirect(url_for("drivers.index"))
    with runtime.lock:
        runtime.registry.unblock(driver_id)
        runtime.registry.register(driver_id, chat_id, runtime.registry.group_title(chat_id) or "",
                                  "manual", name)
        runtime.registry.save()
    flash(f"Assigned {name} to {runtime.registry.group_title(chat_id) or chat_id}.", "ok")
    return redirect(url_for("drivers.index"))
