"""Admin website. Changes made here are saved with source='web':
they appear only in the website history, never as messages in Telegram."""
import hmac
import secrets
from datetime import date, timedelta
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from . import texts as T
from .db import OPEN, STATUSES


def create_web(cfg, db, notifier=None):
    app = Flask(__name__)
    app.secret_key = cfg.secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

    def notify(kind, task_id):
        if notifier:
            (notifier.notify_new_task if kind == "new" else notifier.notify_refresh)(task_id)

    # ---------- security ----------
    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("admin"):
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return wrapper

    @app.before_request
    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400, "Form expired. Go back and try again.")

    @app.context_processor
    def helpers():
        members = {m["id"]: m for m in db.members(active_only=False)}
        return dict(T=T, members=members, name_of=lambda i: T.name_of(members, i), csrf=session.get("csrf", ""),
                    today=db.today(), STATUSES=STATUSES, overdue=lambda t: T.is_overdue(t, db.today()))

    # ---------- pages ----------
    @app.get("/health")
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", ""), cfg.dashboard_password):
                session.clear()
                session["admin"] = True
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("tasks"))
            error = "Wrong password."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        return redirect(url_for("tasks"))

    @app.get("/tasks")
    @login_required
    def tasks():
        rows = db.tasks()
        today = db.today()
        show = request.args.get("show", "open")
        who = request.args.get("who", "")
        q = request.args.get("q", "").strip().lower()
        stats = {
            "Open": sum(t["status"] in OPEN for t in rows),
            "In progress": sum(t["status"] == "progress" for t in rows),
            "Waiting": sum(t["status"] == "blocked" for t in rows),
            "Needs approval": sum(t["status"] == "approval" for t in rows),
            "Overdue": sum(T.is_overdue(t, today) for t in rows),
            "Done": sum(t["status"] == "done" for t in rows),
            "Rejected": sum(t["status"] == "rejected" for t in rows),
        }
        if show == "open":
            rows = [t for t in rows if t["status"] not in ("done", "rejected")]
        elif show == "overdue":
            rows = [t for t in rows if T.is_overdue(t, today)]
        elif show in STATUSES:
            rows = [t for t in rows if t["status"] == show]
        if who:
            rows = [t for t in rows if t["assignee"] == who]
        if q:
            rows = [t for t in rows if q in (t["title"] + " " + t["company"] + " " + t["description"]).lower()
                    or q.lstrip("#t-0") == str(t["id"])]
        return render_template("tasks.html", rows=list(reversed(rows)), stats=stats, show=show, who=who, q=q)

    def form_values(f):
        return {
            "title": f.get("title", "").strip(),
            "description": f.get("description", "").strip(),
            "assignee": f.get("assignee", ""),
            "deadline": f.get("deadline", ""),
            "priority": f.get("priority", ""),
            "company": f.get("company", "").strip(),
            "category": f.get("category", ""),
            "notes": f.get("notes", "").strip(),
        }

    def validate(v, new=False):
        errors = []
        if not v["title"]:
            errors.append("Title is required.")
        if v["assignee"] != T.ALL and not db.get_member(v["assignee"]):
            errors.append("Pick an assignee.")
        try:
            d = date.fromisoformat(v["deadline"])
            if new and d.isoformat() < db.today():
                errors.append("The deadline cannot be in the past.")
        except ValueError:
            errors.append("Pick a deadline date.")
        if v["priority"] not in T.PRIORITY:
            errors.append("Pick a priority.")
        if v["category"] not in T.CATEGORIES:
            errors.append("Pick a category.")
        err = T.company_error(v["company"])
        if err:
            errors.append("Company: " + err)
        return errors

    @app.route("/tasks/new", methods=["GET", "POST"])
    @login_required
    def task_new():
        v = form_values(request.form) if request.method == "POST" else {"priority": "med", "category": T.CATEGORIES[0]}
        creator = request.form.get("creator", "")
        errors = []
        if request.method == "POST":
            errors = validate(v, new=True)
            if not db.get_member(creator):
                errors.append("Pick who creates the task.")
            if not errors:
                task_id = db.create_task(v, creator.upper(), "web")
                notify("new", task_id)
                flash(f"Task #{T.code(task_id)} created and posted in the team group.")
                return redirect(url_for("task_edit", task_id=task_id))
        return render_template("task_form.html", v=v, errors=errors, creator=creator, task=None)

    @app.route("/tasks/<int:task_id>", methods=["GET", "POST"])
    @login_required
    def task_edit(task_id):
        t = db.get_task(task_id)
        if not t:
            abort(404)
        errors = []
        if request.method == "POST":
            action = request.form.get("action", "save")
            if action == "note":
                text = request.form.get("note", "").strip()
                if text:
                    db.add_history(task_id, "WEB", "Website note: " + text, source="web")
                    flash("Note saved in the website history.")
                return redirect(url_for("task_edit", task_id=task_id))
            v = form_values(request.form)
            errors = validate(v)
            status = request.form.get("status", t["status"])
            reason = request.form.get("reason", "").strip()
            if status not in STATUSES:
                errors.append("Pick a status.")
            if status in ("blocked", "rejected") and not reason:
                errors.append("Waiting / Rejected needs a reason.")
            if not errors:
                members = {m["id"]: m for m in db.members(active_only=False)}
                changed = 0
                for key in ("title", "description", "assignee", "deadline", "priority", "company", "category", "notes"):
                    if v[key] != t[key]:
                        label = T.STEP_BY_KEY[key]["label"]
                        old, new = T.field_value(members, key, t[key]), T.field_value(members, key, v[key])
                        db.update_field(task_id, key, v[key], "WEB", "web", history_text=f"Changed {label}: {old} → {new}")
                        changed += 1
                if status != t["status"] or (status in ("blocked", "rejected") and reason != t["reason"]):
                    db.set_status(task_id, status, "WEB", "web", reason=reason if status in ("blocked", "rejected") else None,
                                  history_text=f"Status: {T.STATUS[t['status']]} → {T.STATUS[status]}"
                                               + (f" · reason: {reason}" if status in ("blocked", "rejected") else ""))
                    changed += 1
                if changed:
                    notify("refresh", task_id)
                    flash(f"Saved {changed} change(s). Only this website history shows them.")
                else:
                    flash("Nothing changed.")
                return redirect(url_for("task_edit", task_id=task_id))
            t = dict(t, **v, status=status, reason=reason)
        return render_template("task_form.html", v=t, errors=errors, task=t,
                               history=db.history(task_id, include_web=True), creator=t["creator"])

    @app.get("/history")
    @login_required
    def history():
        return render_template("history.html", rows=db.all_history(700))

    @app.route("/members", methods=["GET", "POST"])
    @login_required
    def members_page():
        errors = []
        if request.method == "POST":
            mid = request.form.get("id", "").strip().upper()
            name = request.form.get("name", "").strip()
            if not (2 <= len(mid) <= 10 and mid.isalnum()):
                errors.append("Employee ID can have only letters and numbers, like K150.")
            elif db.get_member(mid):
                errors.append(f"ID {mid} already exists.")
            if not name:
                errors.append("Name is required.")
            if not errors:
                db.add_member(mid, name, request.form.get("role") == "admin", request.form.get("tg_username", ""))
                flash(f"{name} ({mid}) added. They must send {mid} to the bot once to link Telegram.")
                return redirect(url_for("members_page"))
        rows = db.members(active_only=False)
        tasks = db.tasks()
        counts = {m["id"]: (sum(t["assignee"] == m["id"] and t["status"] in OPEN for t in tasks),
                            sum(t["status"] == "done" and (t["done_by"] or t["assignee"]) == m["id"] for t in tasks))
                  for m in rows}
        return render_template("members.html", rows=rows, counts=counts, errors=errors)

    @app.post("/members/<member_id>")
    @login_required
    def member_update(member_id):
        m = db.get_member(member_id)
        if not m:
            abort(404)
        if request.form.get("action") == "unlink":
            db.unlink_member(m["id"])
            flash(f"{m['name']} is unlinked. They can send their ID to the bot again.")
        else:
            db.update_member(m["id"], request.form.get("name", m["name"]) or m["name"],
                             request.form.get("role") == "admin", request.form.get("tg_username", ""),
                             request.form.get("active") == "1")
            flash(f"{m['name']} saved.")
        return redirect(url_for("members_page"))

    return app
