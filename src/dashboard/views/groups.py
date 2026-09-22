"""Groups — every Telegram group the bot can see, and who it belongs to.

This page exists so linking a fleet never means running a command per driver.
The bot already sees each group (privacy mode is off), so the only missing
piece is the title/driver mapping — and registry.best_match works that out.
Whatever it is sure about is linked automatically by the command loop; whatever
is left shows up here with its best lead and a single button that links them
all at once.

Suggestions are recomputed live against the current roster rather than read
back from the pending queue: a group that was ambiguous last week (two drivers
matched) resolves by itself once one of them leaves the roster.
"""

from __future__ import annotations

import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.registry import best_match

from ..auth import login_required
from .drivers import _live_candidates

bp = Blueprint("groups", __name__, url_prefix="/groups")


def _unlinked_rows(runtime, candidates):
    """Known groups with no driver, each with its best current match.

    A lead pointing at a driver who is already linked elsewhere, or who was
    explicitly removed, is dropped — one click must never steal a working link.
    """
    registry = runtime.registry
    by_id = {c.driver_id: c for c in candidates}
    rows = []
    for chat_id, rec in registry.known_groups().items():
        if registry.drivers_for_chat(chat_id):
            continue
        title = rec.get("title") or ""
        match = best_match(title, candidates)
        driver_id = match.driver_id
        if driver_id and (registry.is_registered(driver_id) or registry.is_blocked(driver_id)):
            driver_id = None
        rows.append({
            "chat_id": chat_id,
            "title": title or "(no title)",
            "last_seen": rec.get("last_seen") or "",
            "driver_id": driver_id,
            "driver_name": by_id[driver_id].name if driver_id else "",
            "company": by_id[driver_id].company if driver_id else "",
            "confidence": match.confidence if driver_id else "none",
            "reason": match.reason,
        })
    # Best leads first, then alphabetically — the linkable ones are the point.
    order = {"high": 0, "medium": 1, "none": 2}
    rows.sort(key=lambda r: (order.get(r["confidence"], 3), r["title"].lower()))
    return rows


def _storage_warning() -> str | None:
    """Warn when every link on this page dies at the next redeploy.

    Linking is worthless if it does not survive a deploy, and the failure is
    silent: the registry file just comes back empty and the fleet goes quiet
    again. DATA_DIR pointing at a path that is not a real mount is exactly that
    situation.
    """
    data_dir = os.environ.get("DATA_DIR", ".")
    if data_dir in (".", "", "./") or not os.path.isabs(data_dir):
        return None  # local run — the file sits in the working directory
    try:
        if os.path.ismount(data_dir):
            return None
    except OSError:
        pass
    return (f"DATA_DIR={data_dir} is not a mounted volume — every link below is "
            f"erased on the next redeploy. On Railway: Settings → Volumes → add "
            f"a volume mounted at {data_dir}.")


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    candidates, errors = _live_candidates(runtime)
    rows = _unlinked_rows(runtime, candidates)
    linked = runtime.registry.count()

    unlinked_drivers = [c for c in candidates if not runtime.registry.chat_for(c.driver_id)]
    return render_template(
        "groups.html",
        rows=rows,
        suggested=[r for r in rows if r["driver_id"]],
        linked=linked,
        total_drivers=len(candidates),
        unlinked_drivers=sorted(unlinked_drivers, key=lambda c: c.name),
        roster_errors=errors,
        storage_warning=_storage_warning(),
    )


@bp.post("/link")
@login_required
def link():
    """Link one group to one driver (the row button, or the manual dropdown)."""
    runtime = current_app.config["RUNTIME"]
    chat_id = request.form.get("chat_id", "").strip()
    driver_id = request.form.get("driver_id", "").strip()
    if not chat_id or not driver_id:
        flash("Pick a driver for the group first.", "error")
        return redirect(url_for("groups.index"))

    candidates, _ = _live_candidates(runtime)
    driver = next((c for c in candidates if c.driver_id == driver_id), None)
    if driver is None:
        flash("That driver is no longer on the roster.", "error")
        return redirect(url_for("groups.index"))

    title = runtime.registry.group_title(chat_id) or ""
    with runtime.lock:
        runtime.registry.unblock(driver_id)
        runtime.registry.register(driver_id, chat_id, title, "dashboard", driver.name)
        runtime.registry.clear_pending(chat_id)
        runtime.registry.save()
    flash(f"{driver.name} now gets alerts in {title or chat_id}.", "ok")
    return redirect(url_for("groups.index"))


@bp.post("/link-all")
@login_required
def link_all():
    """Accept every suggestion on the page in one go.

    Each driver is claimed at most once per run, so two groups leading to the
    same driver cannot fight over them — the second is left for a human.
    """
    runtime = current_app.config["RUNTIME"]
    candidates, _ = _live_candidates(runtime)
    by_id = {c.driver_id: c for c in candidates}
    wanted = set(request.form.getlist("confidence") or ["high", "medium"])

    linked, taken = 0, set()
    with runtime.lock:
        for row in _unlinked_rows(runtime, candidates):
            driver_id = row["driver_id"]
            if not driver_id or row["confidence"] not in wanted or driver_id in taken:
                continue
            runtime.registry.unblock(driver_id)
            runtime.registry.register(driver_id, row["chat_id"], row["title"],
                                      "dashboard-bulk", by_id[driver_id].name)
            runtime.registry.clear_pending(row["chat_id"])
            taken.add(driver_id)
            linked += 1
        if linked:
            runtime.registry.save()

    if linked:
        flash(f"Linked {linked} group(s). Those drivers start getting alerts "
              f"on the next poll cycle.", "ok")
    else:
        flash("Nothing to link — no group has a confident match right now.", "error")
    return redirect(url_for("groups.index"))
