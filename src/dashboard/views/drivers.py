from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.eld import ELDError, build_provider
from src.rules import (KIND_CYCLE, KIND_DISCONNECT, KIND_LOW_HOURS, KIND_ON_DUTY,
                       KIND_SHIFT_VIOLATION)

from ..auth import login_required

bp = Blueprint("drivers", __name__, url_prefix="/drivers")

# Upgrade spec §4.2 — the Edit modal's per-kind toggles and language chip.
ALERT_KINDS = [
    (KIND_LOW_HOURS, "Low hours"),
    (KIND_CYCLE, "Cycle"),
    (KIND_SHIFT_VIOLATION, "Shift violation"),
    (KIND_DISCONNECT, "Disconnect"),
    (KIND_ON_DUTY, "On-duty"),
]
LANGUAGES = [("en", "English"), ("ru", "Russian"), ("uz", "Uzbek"), ("es", "Spanish")]


def _live_candidates(runtime):
    """Roster for the manual-assign dropdown.

    Reads the shared RosterCache (primed by the scheduler every cycle) so
    opening this page does NOT fire its own DriveHOS fetch — the provider key is
    shared and uncoordinated fetches trip the rate limit. Falls back to a direct
    fetch only if no cache is wired.
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


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    candidates, errors = _live_candidates(runtime)
    records = runtime.registry.all()
    unassigned = [c for c in candidates if c.driver_id not in records]
    blocked_ids = {did for did in records if runtime.registry.is_blocked(did)}

    company_filter = request.args.get("company", "").strip()
    if company_filter:
        in_company = {c.driver_id for c in candidates if c.company == company_filter}
        records = {did: rec for did, rec in records.items() if did in in_company}
        unassigned = [c for c in unassigned if c.company == company_filter]

    return render_template(
        "drivers.html",
        records=records,
        coverage=runtime.registry.coverage(),
        candidates=candidates,
        unassigned=unassigned,
        blocked_ids=blocked_ids,
        roster_errors=errors,
        alert_kinds=ALERT_KINDS,
        languages=LANGUAGES,
        company_filter=company_filter,
    )


@bp.post("/<driver_id>/unregister")
@login_required
def unregister(driver_id):
    runtime = current_app.config["RUNTIME"]
    name = runtime.registry.driver_name(driver_id) or driver_id
    with runtime.lock:
        runtime.registry.unregister(driver_id)
        runtime.registry.block(driver_id)  # stays out even if title auto-match would re-add
        runtime.registry.save()
    flash(f"Removed {name} — no more alerts until re-added.", "ok")
    return redirect(url_for("drivers.index"))


@bp.post("/<driver_id>/unblock")
@login_required
def unblock(driver_id):
    runtime = current_app.config["RUNTIME"]
    with runtime.lock:
        runtime.registry.unblock(driver_id)
        runtime.registry.save()
    flash("Driver unblocked — auto-matching can register them again.", "ok")
    return redirect(url_for("drivers.index"))


@bp.post("/assign")
@login_required
def assign():
    runtime = current_app.config["RUNTIME"]
    driver_id = request.form.get("driver_id", "").strip()
    name = request.form.get("driver_name", "").strip()
    chat_id = request.form.get("chat_id", "").strip()
    if not driver_id or not name or not chat_id:
        flash("Driver, and chat id are required.", "error")
        return redirect(url_for("drivers.index"))
    with runtime.lock:
        runtime.registry.unblock(driver_id)
        runtime.registry.register(driver_id, chat_id, "", "manual", name)
        runtime.registry.save()
    flash(f"Assigned {name} to chat {chat_id}.", "ok")
    return redirect(url_for("drivers.index"))


@bp.post("/<driver_id>/edit")
@login_required
def edit(driver_id):
    """Save the Edit modal (§4.2): ELD + Dispatch chat, language, kind toggles.

    Works for an unregistered roster driver too ("Add driver" per §4.4 is
    assigning an EXISTING roster driver into monitoring, not creating one) —
    update_driver_settings creates a bare record if none exists yet.
    """
    runtime = current_app.config["RUNTIME"]
    name = request.form.get("driver_name", "").strip()
    chat_id = request.form.get("chat_id", "").strip()
    dispatch_chat_id = request.form.get("dispatch_chat_id", "").strip()
    language = request.form.get("language", "en").strip() or "en"
    enabled_kinds = set(request.form.getlist("kinds"))
    disabled_kinds = [k for k, _ in ALERT_KINDS if k not in enabled_kinds]

    if not chat_id:
        flash("An ELD group chat id is required.", "error")
        return redirect(url_for("drivers.index"))

    with runtime.lock:
        runtime.registry.unblock(driver_id)
        runtime.registry.update_driver_settings(
            driver_id, driver_name=name or None, chat_id=chat_id,
            dispatch_chat_id=dispatch_chat_id or None,
            language=language, disabled_kinds=disabled_kinds,
        )
        runtime.registry.save()
    flash(f"Saved settings for {name or driver_id}.", "ok")
    return redirect(url_for("drivers.index"))
