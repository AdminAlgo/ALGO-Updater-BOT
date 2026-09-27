"""ALGO GROUP · CASER admin website: one main account that can see and change everything.

Changes made here are saved with source='web': they appear only in the website history,
never as messages in Telegram."""
import hmac
import secrets
import time
from collections import Counter
from datetime import date, timedelta
from functools import wraps

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import report as R
from . import texts as T
from .db import OPEN, STATUSES

MAX_FAILS, LOCK_SECONDS = 5, 600


def create_web(cfg, db, notifier=None):
    app = Flask(__name__)
    app.secret_key = cfg.secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    fails = {}  # ip -> [count, first_fail_time]

    def notify(kind, task_id):
        if notifier:
            (notifier.notify_new_task if kind == "new" else notifier.notify_refresh)(task_id)

    # ---------- account ----------
    def account_name():
        return db.get_setting("web_user") or cfg.dashboard_username

    def check_credentials(user, password):
        stored_hash = db.get_setting("web_pass_hash")
        if not hmac.compare_digest(user.strip().lower(), account_name().lower()):
            return False
        if stored_hash:
            return check_password_hash(stored_hash, password)
        return hmac.compare_digest(password, cfg.dashboard_password)

    def client_ip():
        return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")

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

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.context_processor
    def helpers():
        members = {m["id"]: m for m in db.members(active_only=False)}
        today = db.today()
        return dict(T=T, members=members, name_of=lambda i: "Website" if i == "WEB" else T.name_of(members, i),
                    csrf=session.get("csrf", ""), today=today, STATUSES=STATUSES, account=account_name(),
                    overdue=lambda t: T.is_overdue(t, today), plain=R.PLAIN_STATUS, clean=R.clean)

    # ---------- pages ----------
    @app.get("/health")
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            ip = client_ip()
            count, first = fails.get(ip, [0, time.time()])
            if count >= MAX_FAILS and time.time() - first < LOCK_SECONDS:
                error = "Too many wrong tries. Wait 10 minutes and try again."
            elif check_credentials(request.form.get("username", ""), request.form.get("password", "")):
                fails.pop(ip, None)
                session.clear()
                session["admin"] = True
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
            else:
                if time.time() - first >= LOCK_SECONDS:
                    count, first = 0, time.time()
                fails[ip] = [count + 1, first]
                error = "Wrong username or password."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        tasks = db.tasks()
        today = db.today()
        month_start = today[:8] + "01"
        rows = db.history_between(month_start, "9999")
        done_month = Counter()
        for r in rows:
            if "→ ✅ Done" in r["text"]:
                t = db.get_task(r["task_id"])
                if t:
                    done_month[t["done_by"] or t["assignee"]] += 1
        stats = [
            ("Open", sum(t["status"] in OPEN for t in tasks), "green", url_for("tasks", show="open")),
            ("In progress", sum(t["status"] == "progress" for t in tasks), "blue", url_for("tasks", show="progress")),
            ("Waiting", sum(t["status"] == "blocked" for t in tasks), "amber", url_for("tasks", show="blocked")),
            ("Needs approval", sum(t["status"] == "approval" for t in tasks), "violet", url_for("tasks", show="approval")),
            ("Overdue", sum(T.is_overdue(t, today) for t in tasks), "red", url_for("tasks", show="overdue")),
            ("Done this month", sum(done_month.values()), "green", url_for("reports", kind="month")),
        ]
        attention = [t for t in tasks if T.is_overdue(t, today) or t["status"] == "approval"]
        attention.sort(key=lambda t: (t["status"] != "approval", t["deadline"]))
        status_counts = Counter(t["status"] for t in tasks)
        workload = []
        for m in db.members():
            mine = [t for t in tasks if t["assignee"] == m["id"]]
            open_n = sum(t["status"] in OPEN + ("approval",) for t in mine)
            over_n = sum(T.is_overdue(t, today) for t in mine)
            if open_n or over_n or done_month[m["id"]]:
                workload.append((m, open_n, over_n, done_month[m["id"]]))
        team = [t for t in tasks if t["assignee"] == T.ALL and t["status"] in OPEN + ("approval",)]
        return render_template("dashboard.html", stats=stats, attention=attention[:12], recent=db.all_history(14),
                               status_counts=status_counts, total=len(tasks), workload=workload, team_open=len(team))

    @app.get("/tasks")
    @login_required
    def tasks():
        rows = db.tasks()
        today = db.today()
        show = request.args.get("show", "open")
        who = request.args.get("who", "")
        q = request.args.get("q", "").strip().lower()
        counts = {
            "open": sum(t["status"] not in ("done", "rejected") for t in rows),
            "overdue": sum(T.is_overdue(t, today) for t in rows),
            "approval": sum(t["status"] == "approval" for t in rows),
            "blocked": sum(t["status"] == "blocked" for t in rows),
            "done": sum(t["status"] == "done" for t in rows),
            "rejected": sum(t["status"] == "rejected" for t in rows),
            "all": len(rows),
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
            num = q.replace("#", "").replace("t-", "").lstrip("0")
            rows = [t for t in rows if q in (t["title"] + " " + t["company"] + " " + t["description"]).lower()
                    or (num and num == str(t["id"]))]
        return render_template("tasks.html", rows=list(reversed(rows)), counts=counts, show=show, who=who, q=q)

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
            if request.form.get("action") == "note":
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

    @app.get("/tasks/<int:task_id>/pdf")
    @login_required
    def task_pdf(task_id):
        if not db.get_task(task_id):
            abort(404)
        return pdf_response(R.task_pdf(db, task_id), f"ALGO-CASER-{T.code(task_id)}.pdf")

    @app.get("/history")
    @login_required
    def history():
        period = request.args.get("period", "")
        source = request.args.get("source", "")
        q = request.args.get("q", "").strip().lower()
        label = "Latest 500 changes"
        rows = None
        if ":" in period:
            kind, value = period.split(":", 1)
            try:
                start, end, label = R.period_bounds(kind, value)
                rows = list(reversed(db.history_between(start, end)))
            except ValueError:
                period = ""
        if rows is None:
            rows = db.all_history(500)
        if source in ("bot", "web"):
            rows = [r for r in rows if r["source"] == source]
        if q:
            num = q.replace("#", "").replace("t-", "").lstrip("0")
            rows = [r for r in rows if q in r["text"].lower() or (num and num == str(r["task_id"]))]
        tasks = {t["id"]: t for t in db.tasks()}
        today = date.fromisoformat(db.today())
        return render_template("history.html", rows=rows, tasks=tasks, period=period, source=source, q=q,
                               label=label, options={k: R.period_options(k, today) for k in R.KINDS})

    @app.get("/reports")
    @login_required
    def reports():
        today = date.fromisoformat(db.today())
        kind = request.args.get("kind", "month")
        if kind not in R.KINDS:
            kind = "month"
        value = request.args.get("value") or R.current_value(kind, today)
        try:
            summary = R.summarize(db, kind, value)
        except ValueError:
            abort(400, "Unknown period.")
        return render_template("reports.html", kind=kind, value=value, s=summary,
                               options={k: R.period_options(k, today) for k in R.KINDS})

    @app.get("/reports/pdf")
    @login_required
    def report_pdf():
        kind, value = request.args.get("kind", ""), request.args.get("value", "")
        try:
            data = R.period_pdf(db, kind, value, include_history=request.args.get("history", "1") == "1")
        except ValueError:
            abort(400, "Unknown period.")
        return pdf_response(data, f"ALGO-CASER-{kind}-{value}.pdf")

    def pdf_response(data, filename):
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.route("/members", methods=["GET", "POST"])
    @login_required
    def members_page():
        errors = []
        if request.method == "POST":
            mid = request.form.get("id", "").strip().upper()
            name = request.form.get("name", "").strip()
            if not (2 <= len(mid) <= 12 and mid.isalnum()):
                errors.append("Employee ID can have only letters and numbers, like K150.")
            elif db.get_member(mid):
                errors.append(f"ID {mid} already exists.")
            if not name:
                errors.append("Name is required.")
            if not errors:
                db.add_member(mid, name, request.form.get("role") == "admin", request.form.get("tg_username", ""))
                flash(f"{name} ({mid}) added.")
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
            flash(f"{m['name']} is unlinked from Telegram.")
        else:
            db.update_member(m["id"], request.form.get("name", m["name"]) or m["name"],
                             request.form.get("role") == "admin", request.form.get("tg_username", ""),
                             request.form.get("active") == "1")
            flash(f"{m['name']} saved.")
        return redirect(url_for("members_page"))

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account_page():
        errors = []
        if request.method == "POST":
            new_user = request.form.get("username", "").strip()
            new_pw, again = request.form.get("new_password", ""), request.form.get("again", "")
            if not check_credentials(account_name(), request.form.get("current", "")):
                errors.append("Current password is wrong.")
            if not (3 <= len(new_user) <= 40) or not new_user.replace("_", "").replace(".", "").isalnum():
                errors.append("Username: 3–40 letters, numbers, dot or underscore.")
            if new_pw and len(new_pw) < 10:
                errors.append("New password must have at least 10 characters.")
            if new_pw != again:
                errors.append("The two new passwords are different.")
            if not errors:
                db.set_setting("web_user", new_user)
                if new_pw:
                    db.set_setting("web_pass_hash", generate_password_hash(new_pw))
                elif not db.get_setting("web_pass_hash"):
                    db.set_setting("web_pass_hash", generate_password_hash(cfg.dashboard_password))
                flash("Account saved. Use the new details next time you sign in.")
                return redirect(url_for("account_page"))
        return render_template("account.html", errors=errors)

    return app
