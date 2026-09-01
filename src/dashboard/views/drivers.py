from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.eld import ELDError, build_provider

from ..auth import login_required

bp = Blueprint("drivers", __name__, url_prefix="/drivers")


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
        return [{"driver_id": c.driver_id, "name": c.name} for c in cands], errors

    config = runtime.current_config()
    candidates: list[dict] = []
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
        for snap in result.snapshots:
            candidates.append({"driver_id": snap.driver_id, "name": snap.name})
    return candidates, errors


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    candidates, errors = _live_candidates(runtime)
    return render_template(
        "drivers.html",
        records=runtime.registry.all(),
        coverage=runtime.registry.coverage(),
        candidates=candidates,
        roster_errors=errors,
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
