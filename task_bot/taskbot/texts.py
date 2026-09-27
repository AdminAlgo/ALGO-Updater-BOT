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
    "Commands: /newtask /tasks /my /overdue /report /faq /help /cancel"
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


# ---------- FAQ (shown with the ❓ FAQ button and /faq) ----------
# Placeholders: {times} check-in times, {remind} / {escalate} minutes, {overdue} hours, {admins} admin names.
FAQ = [
    ("What is this bot?",
     "🤖 <b>ALGO Task Bot</b> keeps all team tasks in one place.\n\n"
     "• Every task has its own message in this group with buttons.\n"
     "• The bot reminds people about deadlines.\n"
     "• Every change is saved in the history, so nothing is lost."),
    ("Do I need to register?",
     "🙂 <b>No.</b> You do not need to send any ID.\n\n"
     "The bot knows you by your Telegram account. Just press the buttons under your tasks in this group.\n"
     "You do not need a private chat with the bot."),
    ("How do I see my tasks?",
     "📋 Press <b>Task list</b> (in the pinned message at the top of the group).\n\n"
     "• <b>👤 By person</b>: pick your name to see only your tasks.\n"
     "• <b>🔴 Overdue</b>: tasks that are late.\n"
     "• <b>🔎 Open a task</b>: shows the full task card with its buttons.\n\n"
     "You can also type /my in the group."),
    ("What do the buttons mean?",
     "Buttons under a task:\n\n"
     "▶️ <b>Start work</b>: I am working on it now.\n"
     "⏸ <b>Blocked</b>: I am waiting for someone (driver, customer, other team). The bot asks why.\n"
     "✅ <b>Done</b>: I finished. An admin will check it.\n"
     "⛔ <b>Reject</b>: this task will not be done. The bot asks why.\n"
     "✏️ <b>Edit</b>: change the title, deadline, company or other details.\n"
     "📝 <b>Note</b>: write a short update.\n"
     "📜 <b>History</b>: see every change: who, what and when."),
    ("What do the colors mean?",
     "Task status:\n\n"
     "🆕 <b>New</b>: created, nobody started yet.\n"
     "🔵 <b>In progress</b>: someone is working on it.\n"
     "⏸ <b>Waiting / Blocked</b>: stopped, waiting for someone else.\n"
     "🟣 <b>Needs approval</b>: marked done, an admin must check.\n"
     "✅ <b>Done</b>: approved and closed.\n"
     "⛔ <b>Rejected</b>: will not be done.\n"
     "🔴 <b>OVERDUE</b>: the deadline day has passed.\n\n"
     "Priority: 🟢 Low · 🟡 Medium · 🟠 High · 🔴 Urgent"),
    ("What happens at check-in time?",
     "🕔 Every day at <b>{times}</b> (Tashkent time) the bot asks about every open task.\n\n"
     "Under each task press one button:\n"
     "🔵 <b>Still in progress</b> · ⏸ <b>Blocked</b> · ✅ <b>Done</b> · ⛔ <b>Reject</b>\n\n"
     "You can also press 📝 <b>Add note</b> to explain what you did today."),
    ("What if I do not answer?",
     "⏰ After <b>{remind} minutes</b> without an answer, the bot reminds you.\n"
     "🚨 After <b>{escalate} minutes</b>, the bot tells the admins ({admins}).\n\n"
     "So please answer the check-in on time. One button press is enough."),
    ("My task is late. What do I do?",
     "🔴 A task is <b>overdue</b> when its deadline day has passed and it is not closed.\n\n"
     "• If you are still working on it, press 🔵 <b>Still in progress</b> and <b>write why</b> it is late. "
     "The note is required.\n"
     "• If you need more time, ask an admin to change the deadline (✏️ Edit → Deadline).\n"
     "• The bot reminds about overdue tasks every <b>{overdue} hours</b> until they are closed."),
    ("I finished my task. What now?",
     "✅ Press <b>Done</b> under the task.\n\n"
     "The status becomes 🟣 <b>Needs approval</b> and the admins get a message. "
     "An admin checks your work and:\n"
     "• approves it: the task is ✅ <b>Done</b> and closed, or\n"
     "• says <b>not completed yet</b>: the task goes back to 🔵 In progress and you continue."),
    ("I am waiting for someone.",
     "⏸ Press <b>Blocked</b> under the task.\n\n"
     "Pick the reason (driver, customer, other team, ELD support) or press ✍️ <b>Type my own</b> and write it.\n"
     "When you can continue, press ▶️ <b>Resume work</b>."),
    ("How do I write an update?",
     "📝 Press <b>Note</b> under the task (or 📝 <b>Add note</b> at check-in), then send your text as a normal message.\n\n"
     "Everyone sees it, and it is saved in the task history."),
    ("Who creates tasks?",
     "➕ Tasks are created by the admins ({admins}) and the official ALGO account.\n\n"
     "They fill a short form in private chat with the bot. The task then appears in this group, "
     "and the person it is for is tagged."),
    ("Who approves tasks?",
     "✅ Only <b>{admins}</b>, from their own Telegram accounts.\n\n"
     "They press ✅ <b>Approve</b>, or type in the group:\n"
     "<code>approve T-0040</code> to close the task\n"
     "<code>not completed yet T-0040</code> to send it back to work"),
    ("The bot says I cannot do this.",
     "⛔ Each task can be changed only by:\n"
     "• the person it is for,\n"
     "• the person who created it,\n"
     "• the admins.\n\n"
     "Tasks for <b>👥 All team members</b> can be answered by anyone. "
     "If a task should be yours, ask an admin to change the assignee."),
    ("What is T-0040?",
     "🔢 It is the <b>task number</b>. Every task gets its own number and it never changes.\n\n"
     "Use it to find a task (🔍 Find by ID) or when admins approve: <code>approve T-0040</code>."),
]
