"""Warnings / Broadcast composer (upgrade spec §4.5).

Sending reuses the existing sender_queue.py / telegram_sender.py path (same
rate limits as the alert engine) — this is a new manual TRIGGER, not a new
delivery mechanism. Every send is written to send_log.jsonl with
kind "broadcast:<template_id>" so it shows up in Statistics (§4.6) too.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..auth import login_required
from ... import send_log
from ...activity_log import ActivityEntry
from ...rules import DRIVER_GROUP, Alert
from ...sender_queue import SendQueue

bp = Blueprint("warnings", __name__, url_prefix="/warnings")

# (code, label) pairs for the template editor's per-language fields — distinct
# from templates_store.LANGUAGES, which is just the flat list of valid codes.
LANGUAGES = [("en", "English"), ("ru", "Russian"), ("uz", "Uzbek"), ("es", "Spanish")]


def _targets(runtime, audience: str, company: str, driver_id: str):
    """Resolve an audience choice to a list of (driver_id, name, chat_id, language)."""
    records = runtime.registry.all()
    cache = getattr(runtime, "roster_cache", None)
    company_of = {c.driver_id: c.company for c in (cache.get() if cache is not None else [])}

    def _row(did, rec):
        return (did, rec.get("driver_name") or did, rec.get("chat_id"),
               rec.get("language") or "en")

    if audience == "driver":
        rec = records.get(driver_id)
        return [_row(driver_id, rec)] if rec and rec.get("chat_id") else []
    if audience == "company":
        return [_row(did, rec) for did, rec in records.items()
                if rec.get("chat_id") and company_of.get(did) == company]
    # "all"
    return [_row(did, rec) for did, rec in records.items() if rec.get("chat_id")]


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    store = runtime.template_store
    return render_template(
        "warnings.html",
        templates=store.all() if store else [],
        companies=[c.name for c in config.companies],
        drivers=sorted(runtime.registry.all().items(), key=lambda kv: kv[1].get("driver_name", "")),
        languages=LANGUAGES,
    )


@bp.post("/templates")
@login_required
def save_template():
    runtime = current_app.config["RUNTIME"]
    store = runtime.template_store
    if store is None:
        flash("Template store is not configured.", "error")
        return redirect(url_for("warnings.index"))
    name = request.form.get("name", "").strip()
    template_id = request.form.get("template_id", "").strip()
    text = {code: request.form.get(f"text_{code}", "") for code, _ in LANGUAGES}
    if not name or not text.get("en", "").strip():
        flash("A name and at least English text are required.", "error")
        return redirect(url_for("warnings.index"))
    if template_id and store.get(template_id):
        store.update(template_id, name=name, text=text)
        flash(f"Updated template “{name}”.", "ok")
    else:
        store.add(name, text)
        flash(f"Saved template “{name}”.", "ok")
    return redirect(url_for("warnings.index"))


@bp.post("/templates/<template_id>/delete")
@login_required
def delete_template(template_id):
    runtime = current_app.config["RUNTIME"]
    store = runtime.template_store
    if store and store.delete(template_id):
        flash("Template deleted.", "ok")
    else:
        flash("Template not found.", "error")
    return redirect(url_for("warnings.index"))


@bp.post("/send")
@login_required
def send():
    runtime = current_app.config["RUNTIME"]
    store = runtime.template_store
    sender = runtime.sender
    if sender is None:
        flash("No Telegram sender is configured for this dashboard.", "error")
        return redirect(url_for("warnings.index"))

    audience = request.form.get("audience", "all")
    company = request.form.get("company", "").strip()
    driver_id = request.form.get("driver_id", "").strip()
    template_id = request.form.get("template_id", "").strip()
    template = store.get(template_id) if store else None
    if not template:
        flash("Choose a saved template to send.", "error")
        return redirect(url_for("warnings.index"))

    targets = _targets(runtime, audience, company, driver_id)
    if not targets:
        flash("No drivers matched that audience.", "error")
        return redirect(url_for("warnings.index"))

    config = runtime.current_config()
    queue = SendQueue(
        sender,
        global_per_second=getattr(config, "send_rate_per_second", 25),
        per_chat_per_minute=getattr(config, "send_rate_per_chat_per_minute", 20),
    )
    kind = f"broadcast:{template.get('name') or template_id}"
    now = datetime.now(timezone.utc)
    for did, name, chat_id, language in targets:
        queue.push(Alert(
            kind=kind, audience=DRIVER_GROUP, chat_id=chat_id, company="",
            driver_name=name, driver_id=did,
            text=store.render(template, language),
        ))

    sent = failed = 0

    def _on_result(alert, res):
        nonlocal sent, failed
        if res.ok:
            sent += 1
        else:
            failed += 1
        runtime.activity_log.record(ActivityEntry(
            ts=now.isoformat(), kind=alert.kind, company=alert.company,
            driver_name=alert.driver_name, chat_id=str(alert.chat_id),
            audience=alert.audience, ok=res.ok, error=res.error,
        ))
        send_log.append(runtime.send_log_path, {
            "ts": now.isoformat(), "kind": alert.kind, "company": alert.company,
            "driver_id": alert.driver_id, "driver_name": alert.driver_name,
            "chat_id": str(alert.chat_id), "audience": alert.audience, "ok": res.ok,
        })

    queue.drain(on_result=_on_result)
    runtime.activity_log.save()
    flash(f"Broadcast sent: {sent} delivered, {failed} failed.", "ok" if not failed else "error")
    return redirect(url_for("warnings.index"))
