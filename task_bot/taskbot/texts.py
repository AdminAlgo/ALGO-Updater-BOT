"""Words, icons and message builders. Pure functions: no Telegram, no database."""
import re
from datetime import date, datetime
from html import escape

STATUS = {
    "new": "🆕 New",
    "progress": "🔵 In progress",
    "blocked": "⏸ Waiting / Blocked",
    "approval": "🟣 Needs approval",
    "done": "✅ Done",
    "rejected": "⛔ Rejected",
}
STATUS_ICON = {k: v.split(" ")[0] for k, v in STATUS.items()}
OPEN = ("new", "progress", "blocked")

PRIORITY = {"low": "🟢 Low", "med": "🟡 Medium", "high": "🟠 High", "urg": "🔴 Urgent"}

CATEGORIES = ["🦺 Safety", "🛠 ELD", "💼 Sales", "💳 Billing", "🎧 Support", "📦 Other"]
CATEGORY_HELP = (
    "🦺 <b>Safety</b>: Audit or DataQ cases\n"
    "🛠 <b>ELD</b>: our department tasks, e.g. from the manager\n"
    "💼 <b>Sales</b>: tasks from management\n"
    "💳 <b>Billing</b>: tasks from accounting\n"
    "🎧 <b>Support</b>: tasks assigned by the manager\n"
    "📦 <b>Other</b>: anything else"
)

BLOCK_REASONS = ["Waiting for the driver", "Waiting for the customer / carrier", "Waiting for another team",
                 "Waiting for ELD provider support"]
REJECT_REASONS = ["Not our department", "Duplicate task", "Not enough information", "Customer cancelled"]

ALL = "ALL"

# The form. `text` steps are typed, the others are buttons.
STEPS = [
    {"key": "title", "label": "Title", "icon": "📌", "text": True, "q": "What needs to be done? Send a short title."},
    {"key": "description", "label": "Description", "icon": "📝", "text": True,
     "q": "Describe the task. Add the details the person needs."},
    {"key": "assignee", "label": "Assignee", "icon": "👤", "q": "Who will do it?"},
    {"key": "deadline", "label": "Deadline", "icon": "⏰", "q": "What is the last day for this task?"},
    {"key": "priority", "label": "Priority", "icon": "⚡", "q": "How important is it?"},
    {"key": "company", "label": "Company", "icon": "🏢", "text": True,
     "q": "Type the company name in <b>CAPITAL LETTERS</b>.\nExample: SEXTON GROUP LTD"},
    {"key": "category", "label": "Category", "icon": "🗂", "q": "Which category?\n\n" + CATEGORY_HELP},
    {"key": "notes", "label": "Notes", "icon": "🗒", "text": True, "skip": True,
     "q": "Any notes? You can change them later. Or press Skip."},
]
STEP_BY_KEY = {s["key"]: s for s in STEPS}

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]


def h(s):
    return escape(str(s or ""), quote=False)


def code(task_id):
    return f"T-{int(task_id):04d}"


def fmt_date(iso, year=True):
    if not iso:
        return "—"
    d = date.fromisoformat(iso)
    out = f"{DOW[d.weekday()]} {d.day} {MON[d.month - 1]}"
    return out + (f" {d.year}" if year else "")


def fmt_stamp(stamp):
    """'2026-09-27 16:45' -> 'Sun 27 Sep 16:45'."""
    if not stamp:
        return "—"
    dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    return f"{DOW[dt.weekday()]} {dt.day} {MON[dt.month - 1]} {dt:%H:%M}"


def company_error(value):
    """'' when the company name is fine, otherwise the problem."""
    if not re.search(r"[A-Za-z]", value or ""):
        return "The name must have letters."
    if re.search(r"[a-z]", value):
        return "The company name must be in CAPITAL LETTERS."
    return ""


def is_overdue(task, today_iso):
    return task["status"] in OPEN and task["deadline"] < today_iso


# ---------- people ----------
def name_of(members, member_id):
    if member_id == ALL:
        return "All team members"
    m = members.get(member_id)
    return m["name"] if m else (member_id or "—")


def mention(members, member_id):
    """Blue name that notifies the person (when their Telegram is linked)."""
    if member_id == ALL:
        return "<b>everyone</b>"
    m = members.get(member_id)
    if not m:
        return h(member_id)
    if m["tg_user_id"]:
        return f'<a href="tg://user?id={m["tg_user_id"]}">{h(m["name"])}</a>'
    return f"<b>{h(m['name'])}</b>"


def admins_mention(members):
    return ", ".join(mention(members, mid) for mid, m in members.items() if m["is_admin"])


def admin_names(members):
    return " or ".join(m["name"] for m in members.values() if m["is_admin"])


def by_line(members, member_id):
    m = members.get(member_id)
    return ("Admin: " if m and m["is_admin"] else "") + h(name_of(members, member_id))


def field_value(members, key, value):
    """How a field value is shown to people."""
    if key == "assignee":
        if value == ALL:
            return "All team members"
        return f"{name_of(members, value)} ({value})" if value else "—"
    if key == "priority":
        return PRIORITY.get(value, "—")
    if key == "deadline":
        return fmt_date(value)
    return value or "—"


# ---------- messages ----------
def card(task, members, today_iso, intro=None):
    t = task
    lines = []
    if intro:
        lines += [intro, ""]
    lines.append(f"📌 <b>#{code(t['id'])} · {h(t['title'])}</b>")
    lines.append(f"Status: <b>{STATUS[t['status']]}</b>" + ("   🔴 <b>OVERDUE</b>" if is_overdue(t, today_iso) else ""))
    if t["assignee"] == ALL:
        lines.append("👤 Assignee: 👥 All team members")
    else:
        lines.append(f"👤 Assignee: {mention(members, t['assignee'])} · {h(t['assignee'])}")
    lines.append(f"⏰ Deadline: {fmt_date(t['deadline'])}")
    lines.append(f"⚡ Priority: {PRIORITY.get(t['priority'], '—')}")
    if t["company"]:
        lines.append(f"🏢 Company: <b>{h(t['company'])}</b>")
    lines.append(f"🗂 Category: {h(t['category'])}")
    if t["description"]:
        lines += ["", f"📝 {h(t['description'])}"]
    if t["notes"]:
        lines.append(f"🗒 Notes: {h(t['notes'])}")
    if t["reason"] and t["status"] in ("blocked", "rejected"):
        lines.append(f"💬 Reason: {h(t['reason'])}")
    if t["last_note"]:
        lines.append(f"🗨 Latest note ({fmt_stamp(t['last_note_at'])}, {h(name_of(members, t['last_note_by']))}): "
                     f"{h(t['last_note'])}")
    lines += ["", f"<i>Created by {by_line(members, t['creator'])} · Last update {fmt_stamp(t['updated_at'])} "
                  f"by {h(name_of(members, t['updated_by']))}</i>"]
    return "\n".join(lines)


def history_text(task, rows, members):
    out = [f"📜 <b>History of #{code(task['id'])}</b>", h(task["title"]), ""]
    for r in rows:
        out.append(f"<i>{fmt_stamp(r['at'])}</i> · {h(name_of(members, r['who']))}\n{h(r['text'])}")
        out.append("")
    return "\n".join(out).strip()


def list_line(task, members):
    who = "All team" if task["assignee"] == ALL else name_of(members, task["assignee"])
    return (f"• <b>#{code(task['id'])}</b> · {h(shorten(task['title'], 44))}\n"
            f"   👤 {h(who)} · ⏰ {fmt_date(task['deadline'], year=False)} · {PRIORITY.get(task['priority'], ' ').split(' ')[0]}")


def task_list(tasks, members, today_iso, now_label):
    over = [t for t in tasks if is_overdue(t, today_iso)]
    by = lambda st: [t for t in tasks if t["status"] == st and not is_overdue(t, today_iso)]
    parts = [f"📋 <b>Task list · {now_label}</b>"]
    for title, group in (("🔴 Overdue", over), ("🟣 Needs approval", by("approval")), ("🔵 In progress", by("progress")),
                         ("⏸ Waiting / Blocked", by("blocked")), ("🆕 New", by("new"))):
        if group:
            parts.append(f"\n<b>{title} ({len(group)})</b>\n" + "\n".join(list_line(t, members) for t in group))
    parts.append(f"\n✅ Done: {len(by('done'))} · ⛔ Rejected: {len(by('rejected'))}")
    return "\n".join(parts)


def shorten(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


HELP = (
    "❓ <b>How it works</b>\n\n"
    "• ➕ <b>New task</b>: fill a short form (8 steps). The task is posted in the team group.\n"
    "• Under every task: Start, Blocked, Done, Reject, Edit, Note, History.\n"
    "• <b>Done</b> must be approved by an admin from their own account:\n"
    "   <code>approve T-0040</code> closes the task\n"
    "   <code>not completed yet T-0040</code> sends it back to work\n"
    "• At <b>{times}</b> I ask about every open task.\n"
    "• No answer in {remind} min: reminder. After {escalate} min: I tell the admins.\n"
    "• Overdue tasks: reminder every {overdue} hours until closed.\n\n"
    "Commands: /newtask /tasks /my /overdue /report /help /cancel"
)


# ---------- typed admin commands ----------
_REF = r"#?\s*(?:t\s*-?\s*)?(\d{1,6})"
_APPROVE = re.compile(r"^\s*approve(?:\s+task)?\s*" + _REF + r"\s*$", re.I)
_NOT_DONE = re.compile(r"^\s*not\s+completed(?:\s+yet)?(?:\s+task)?\s*" + _REF + r"\s*$", re.I)


def parse_admin_command(text):
    """'approve T-0040' -> ('approve', 40); 'not completed yet 40' -> ('not_done', 40); else None."""
    for kind, rx in (("approve", _APPROVE), ("not_done", _NOT_DONE)):
        m = rx.match(text or "")
        if m and int(m.group(1)) > 0:
            return kind, int(m.group(1))
    return None


def parse_task_ref(text):
    m = re.search(r"(\d{1,6})", text or "")
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def looks_like_employee_id(text):
    return bool(re.fullmatch(r"[A-Za-z]{0,3}\d{2,6}", (text or "").strip()))
