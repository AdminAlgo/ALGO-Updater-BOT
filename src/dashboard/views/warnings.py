"""Warnings — send a message (e.g. "inspection week") to every driver, one
company, or one driver, in each driver's own language; manage the message
templates; and edit the wording of the automatic alerts.

Sending reuses the same SendQueue / TelegramSender path as the alert engine
(same rate limits, SAFE_MODE applies). Every send is logged to send_log.jsonl
as kind "broadcast:<template name>" so it shows up in Statistics.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ... import messages, send_log
from ...activity_log import ActivityEntry
from ...rules import DRIVER_GROUP, Alert
from ...sender_queue import SendQueue
from ...templates_store import PLACEHOLDERS
from ..auth import login_required
from ..rows import company_rows
from ._common import LANGUAGES

bp = Blueprint("warnings", __name__, url_prefix="/warnings")


def _choice():
    src = request.form if request.method == "POST" else request.args
    return {
        "audience": src.get("audience", "all"),
        "company": src.get("company", "").strip(),
        "driver_id": src.get("driver_id", "").strip(),
        "template_id": src.get("template_id", "").strip(),
        "dispatch": src.get("dispatch") == "1",
    }


def _targets(runtime, config, choice) -> tuple[list[dict], int]:
    """Live-roster drivers the choice reaches: linked, updates on. Returns
    (rows, how many matched but were skipped for no group / updates off)."""
    rows = company_rows(runtime, config)
    if choice["audience"] == "driver":
        rows = [r for r in rows if r["driver_id"] == choice["driver_id"]]
    elif choice["audience"] == "company":
        rows = [r for r in rows if r["company"] == choice["company"]]
    reachable = [r for r in rows if r["linked"] and not r["paused"]]
    return reachable, len(rows) - len(reachable)


def _page(runtime, config, choice, preview=None):
    store = runtime.template_store
    rows = company_rows(runtime, config)
    texts = getattr(runtime, "alert_texts", None)
    custom = texts.all() if texts is not None else {}
    alert_texts = [{
        "kind": kind, "label": label,
        "placeholders": messages.PLACEHOLDERS[kind],
        "custom": sorted(custom.get(kind, {})),
        "texts": {lang: messages.template_for(kind, lang) for lang in messages.LANGUAGES},
    } for kind, label in messages.KIND_LABELS.items()]
    return render_template(
        "warnings.html",
        templates=store.all() if store else [],
        companies=[c.name for c in config.companies if c.enabled],
        drivers=sorted((r for r in rows if r["linked"]), key=lambda r: r["name"].lower()),
        languages=LANGUAGES, choice=choice, preview=preview,
        placeholders=PLACEHOLDERS, alert_texts=alert_texts,
        open_kind=request.args.get("kind", ""),
    )


@bp.get("")
@login_required
def index():
    runtime = current_app.config["RUNTIME"]
    return _page(runtime, runtime.current_config(), _choice())


@bp.post("/preview")
@login_required
def preview():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    choice = _choice()
    store = runtime.template_store
    template = store.get(choice["template_id"]) if store else None
    if not template:
        flash("Choose a message template first.", "error")
        return redirect(url_for("warnings.index"))
    targets, skipped = _targets(runtime, config, choice)
    by_lang: dict[str, list] = {}
    for r in targets:
        by_lang.setdefault(r["language"], []).append(r)
    samples = [{
        "code": code, "label": label, "count": len(by_lang.get(code, [])),
        "text": store.render(template, code, _values(by_lang[code][0]) if by_lang.get(code)
                             else {"name": "John Smith", "company": "", "truck": ""}),
    } for code, label in LANGUAGES if by_lang.get(code)]
    return _page(runtime, config, choice, preview={
        "template": template, "count": len(targets), "skipped": skipped,
        "dispatch": sum(1 for r in targets if r["dispatch_chat_id"]) if choice["dispatch"] else 0,
        "samples": samples, "names": [r["name"] for r in targets[:12]],
    })


def _values(row: dict) -> dict:
    return {"name": row["name"], "company": row["company"], "truck": row["truck"]}


@bp.post("/send")
@login_required
def send():
    runtime = current_app.config["RUNTIME"]
    config = runtime.current_config()
    store = runtime.template_store
    sender = runtime.sender
    choice = _choice()
    template = store.get(choice["template_id"]) if store else None
    if sender is None or template is None:
        flash("Nothing sent — no Telegram sender or no template.", "error")
        return redirect(url_for("warnings.index"))
    targets, _ = _targets(runtime, config, choice)
    if not targets:
        flash("No driver with a linked group matched — nothing sent.", "error")
        return redirect(url_for("warnings.index"))

    queue = SendQueue(
        sender,
        global_per_second=getattr(config, "send_rate_per_second", 25),
        per_chat_per_minute=getattr(config, "send_rate_per_chat_per_minute", 20),
    )
    kind = f"broadcast:{template.get('name') or template.get('id')}"
    now = datetime.now(timezone.utc)
    for r in targets:
        extras = (r["dispatch_chat_id"],) if choice["dispatch"] and r["dispatch_chat_id"] else ()
        queue.push(Alert(
            kind=kind, audience=DRIVER_GROUP, chat_id=r["chat_id"], company=r["company"],
            driver_name=r["name"], driver_id=r["driver_id"],
            text=store.render(template, r["language"], _values(r)),
            extra_chat_ids=extras,
        ))

    counts = {"sent": 0, "withheld": 0, "failed": 0}

    def _on_result(alert, res):
        suppressed = bool(getattr(res, "suppressed", False))
        counts["sent" if res.ok else ("withheld" if suppressed else "failed")] += 1
        runtime.activity_log.record(ActivityEntry(
            ts=now.isoformat(), kind=alert.kind, company=alert.company,
            driver_name=alert.driver_name, chat_id=str(alert.chat_id),
            audience=alert.audience, ok=res.ok, error=res.error,
        ))
        send_log.append(runtime.send_log_path, {
            "ts": now.isoformat(), "kind": alert.kind, "company": alert.company,
            "driver_id": alert.driver_id, "driver_name": alert.driver_name,
            "chat_id": str(alert.chat_id), "audience": alert.audience, "ok": res.ok,
            "suppressed": suppressed,
        })

    queue.drain(on_result=_on_result)
    runtime.activity_log.save()
    msg = f"“{template.get('name')}” sent to {counts['sent']} driver(s)"
    if counts["withheld"]:
        msg += f", {counts['withheld']} withheld by SAFE_MODE (test platform)"
    if counts["failed"]:
        msg += f", {counts['failed']} FAILED (see Overview → Recent activity)"
    flash(msg + ".", "error" if counts["failed"] else "ok")
    return redirect(url_for("warnings.index"))


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
    text = {code: request.form.get(f"text_{code}", "").replace("\r\n", "\n")
            for code, _ in LANGUAGES}
    if not name or not text.get("en", "").strip():
        flash("A name and the English text are required.", "error")
        return redirect(url_for("warnings.index"))
    if template_id and store.get(template_id):
        store.update(template_id, name=name, text=text)
        flash(f"Updated “{name}”.", "ok")
    else:
        store.add(name, text)
        flash(f"Saved new message type “{name}”.", "ok")
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


@bp.post("/alert-texts/<kind>")
@login_required
def save_alert_text(kind):
    runtime = current_app.config["RUNTIME"]
    store = getattr(runtime, "alert_texts", None)
    if store is None:
        flash("Alert text store is not configured.", "error")
        return redirect(url_for("warnings.index"))
    if request.form.get("reset") == "1":
        store.reset_kind(kind)
        flash(f"{messages.KIND_LABELS.get(kind, kind)}: back to the built-in wording.", "ok")
        return redirect(url_for("warnings.index", kind=kind) + "#alert-texts")
    problems = store.save_kind(kind, {code: request.form.get(f"text_{code}", "")
                                      for code in messages.LANGUAGES})
    if problems:
        flash("Not saved — " + "; ".join(problems), "error")
    else:
        flash(f"{messages.KIND_LABELS.get(kind, kind)}: saved. Used from the next alert on.", "ok")
    return redirect(url_for("warnings.index", kind=kind) + "#alert-texts")
