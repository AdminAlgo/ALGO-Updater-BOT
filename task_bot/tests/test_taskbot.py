import asyncio
import os
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from taskbot import config, texts as T
from taskbot.bot import TaskBot
from taskbot.db import DB
from taskbot.web import create_web

TZ = ZoneInfo("Asia/Tashkent")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 27, 16, 40, tzinfo=TZ)


# ---------------- fakes ----------------
class FakeBot:
    username = "algo_task_bot"

    def __init__(self):
        self.sent, self.edits, self.n = [], [], 100

    async def send_message(self, chat_id, text, **kw):
        self.n += 1
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, markup=kw.get("reply_markup"), message_id=self.n))
        return SimpleNamespace(chat_id=chat_id, message_id=self.n)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append((chat_id, message_id, text))

    async def edit_message_reply_markup(self, **kw):
        pass

    def last(self):
        return self.sent[-1].text

    def buttons(self, i=-1):
        m = self.sent[i].markup
        return [b.callback_data or b.url for row in m.inline_keyboard for b in row] if m else []


class FakeJobs:
    def __init__(self):
        self.once = []

    def run_once(self, cb, when, data):
        self.once.append((cb, data))


GROUP = -100500


@pytest.fixture
def env(tmp_path):
    clock = {"now": NOW}
    db = DB(str(tmp_path / "t.db"), TZ, clock=lambda: clock["now"])
    db.seed_members(os.path.join(HERE, "members_seed.json"))
    db.set_setting("group_chat_id", GROUP)
    cfg = config.Config(token="x", admin_usernames=frozenset({"milord0_0", "abdulakhatov04"}), creator_usernames=frozenset(), tz=TZ,
                        checkin_times=((17, 0), (1, 0)), remind_after_min=30, escalate_after_min=60,
                        overdue_every_hours=3, data_dir=str(tmp_path), port=0, dashboard_username="boss", dashboard_password="pw",
                        secret_key="s" * 32, group_chat_id=None)
    bot = TaskBot(cfg, db)
    bot.app = SimpleNamespace(bot=FakeBot(), job_queue=FakeJobs())
    users = {"N820": (1, "milord0_0"), "N520": (2, "Abdulakhatov04"), "A430": (3, "ali"), "S140": (4, "sara")}
    for mid, (uid, uname) in users.items():
        assert db.link_member(mid, uid, uname) == "ok"
    ctx = {uid: SimpleNamespace(user_data={}) for uid, _ in users.values()}
    return SimpleNamespace(db=db, bot=bot, tg=bot.app.bot, cfg=cfg, users=users, ctx=ctx, clock=clock)


def run(coro):
    return asyncio.run(coro)


def user(env, mid):
    uid, uname = env.users[mid]
    return SimpleNamespace(id=uid, username=uname, full_name=mid, is_bot=False)


def press(env, mid, data, chat_id=GROUP, chat_type="supergroup"):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    u = user(env, mid)
    q = SimpleNamespace(data=data, from_user=u, answer=answer,
                        message=SimpleNamespace(chat_id=chat_id, chat=SimpleNamespace(type=chat_type), message_id=1,
                                                reply_markup=None, text_html=""))
    run(env.bot.on_callback(SimpleNamespace(callback_query=q), env.ctx[u.id]))
    return [a for a in answers if a]


def type_text(env, mid, text, chat_id=GROUP, chat_type="supergroup"):
    u = user(env, mid)
    upd = SimpleNamespace(effective_message=SimpleNamespace(text=text, message_id=7), effective_user=u,
                          effective_chat=SimpleNamespace(id=chat_id, type=chat_type))
    run(env.bot.on_text(upd, env.ctx[u.id]))


def make_task(env, **kw):
    data = dict(title="Check medical cards", description="List drivers", assignee="A430", deadline="2026-09-30",
                priority="high", company="SEXTON GROUP LTD", category="🦺 Safety", notes="")
    data.update(kw)
    return env.db.create_task(data, "N820")


# ---------------- texts ----------------
def test_admin_commands_parse():
    assert T.parse_admin_command("approve T-0040") == ("approve", 40)
    assert T.parse_admin_command("Approve task 40") == ("approve", 40)
    assert T.parse_admin_command("approve #t-0040") == ("approve", 40)
    assert T.parse_admin_command("not completed yet 40") == ("not_done", 40)
    assert T.parse_admin_command("Not completed T-0041") == ("not_done", 41)
    assert T.parse_admin_command("approve") is None
    assert T.parse_admin_command("approve 0") is None
    assert T.parse_admin_command("please approve 40") is None


def test_company_must_be_capitals():
    assert T.company_error("SEXTON GROUP LTD") == ""
    assert T.company_error("A&B LOGISTICS 2 LLC") == ""
    assert "CAPITAL" in T.company_error("Sexton group")
    assert T.company_error("123") != ""


def test_dates():
    assert T.fmt_date("2026-09-25") == "Fri 25 Sep 2026"
    assert T.fmt_stamp("2026-09-27 16:45") == "Sun 27 Sep 16:45"


# ---------------- form ----------------
def test_form_creates_task_and_posts_card(env):
    dm = 1
    run(env.bot.start_form(dm, env.ctx[1]))
    type_text(env, "N820", "Fix ELD disconnect on truck 105", dm, "private")
    type_text(env, "N820", "Tablet loses connection", dm, "private")
    assert "f:pick:2:A430" in env.tg.buttons()
    press(env, "N820", "f:pick:2:A430", dm, "private")
    assert "dl:m:2026-10" in env.tg.buttons()            # 1) month
    press(env, "N820", "dl:m:2026-09", dm, "private")
    days = env.tg.buttons()
    assert "dl:d:2026-09-26" not in days and "dl:d:2026-09-27" in days  # past days are not clickable
    press(env, "N820", "dl:d:2026-09-30", dm, "private")
    assert "Wed 30 Sep 2026" in env.tg.last()            # 3) confirm
    press(env, "N820", "dl:ok:2026-09-30", dm, "private")
    press(env, "N820", "f:pick:4:urg", dm, "private")
    type_text(env, "N820", "sexton group ltd", dm, "private")
    assert "CAPITAL LETTERS" in env.tg.last()
    assert "co:use" in env.tg.buttons()
    press(env, "N820", "co:use", dm, "private")
    press(env, "N820", "f:pick:6:3", dm, "private")      # 💳 Billing
    press(env, "N820", "f:skip:7", dm, "private")
    assert "Check the task before saving" in env.tg.last()
    press(env, "N820", "f:create", dm, "private")
    t = env.db.get_task(1)
    assert (t["assignee"], t["deadline"], t["priority"], t["company"], t["category"]) == \
           ("A430", "2026-09-30", "urg", "SEXTON GROUP LTD", "💳 Billing")
    group_msgs = [m for m in env.tg.sent if m.chat_id == GROUP]
    assert "New task" in group_msgs[-1].text and "#T-0001" in group_msgs[-1].text
    assert env.db.cards(1)


def test_old_form_buttons_are_refused(env):
    run(env.bot.start_form(1, env.ctx[1]))
    type_text(env, "N820", "Title", 1, "private")
    alerts = press(env, "N820", "f:skip:0", 1, "private")
    assert alerts and "old" in alerts[0]


# ---------------- task buttons ----------------
def test_block_asks_reason_once_and_records_it(env):
    tid = make_task(env)
    press(env, "A430", f"c:block:{tid}")
    alerts = press(env, "A430", f"c:block:{tid}")
    assert alerts and "already open" in alerts[0]
    press(env, "A430", f"r:block:{tid}:0")
    t = env.db.get_task(tid)
    assert t["status"] == "blocked" and t["reason"] == "Waiting for the driver"
    assert "is waiting" in env.tg.last()


def test_stranger_cannot_change_task(env):
    tid = make_task(env)
    alerts = press(env, "S140", f"c:start:{tid}")
    assert alerts and "Only the creator" in alerts[0]
    assert env.db.get_task(tid)["status"] == "new"


def test_done_then_typed_approve_only_by_admin(env):
    tid = make_task(env)
    press(env, "A430", f"c:done:{tid}")
    assert env.db.get_task(tid)["status"] == "approval"
    assert f"approve {T.code(tid)}" in env.tg.last()
    type_text(env, "A430", f"approve {tid}")
    assert "Only" in env.tg.last() and env.db.get_task(tid)["status"] == "approval"
    type_text(env, "N820", f"approve T-{tid:04d}")
    assert env.db.get_task(tid)["status"] == "done"
    assert "approved by" in env.tg.last()


def test_not_completed_yet_sends_back(env):
    tid = make_task(env)
    press(env, "A430", f"c:done:{tid}")
    type_text(env, "N520", f"not completed yet {tid}")
    assert env.db.get_task(tid)["status"] == "progress"
    assert "not completed yet" in env.tg.last()


def test_edit_deadline_via_picker(env):
    tid = make_task(env)
    press(env, "N820", f"c:edit:{tid}")
    press(env, "N820", f"e:field:{tid}:deadline")
    press(env, "N820", "dl:m:2026-10")
    press(env, "N820", "dl:d:2026-10-05")
    press(env, "N820", "dl:ok:2026-10-05")
    assert env.db.get_task(tid)["deadline"] == "2026-10-05"
    assert "changed <b>Deadline</b>" in env.tg.last()


# ---------------- check-ins ----------------
def test_checkin_reminder_escalation(env):
    a = make_task(env)
    b = make_task(env, assignee="S140", title="Second")
    run(env.bot.run_checkin("17:00"))
    data = env.bot.app.job_queue.once[0][1]
    press(env, "A430", f"ci:progress:{a}:{data['round']}")
    assert press(env, "A430", f"ci:progress:{a}:{data['round']}") == ["Already answered."]
    run(env.bot.job_remind(SimpleNamespace(job=SimpleNamespace(data=data))))
    assert "Reminder" in env.tg.last() and "Second" in env.tg.last()
    run(env.bot.job_escalate(SimpleNamespace(job=SimpleNamespace(data=data))))
    assert "No update for 60 min" in env.tg.last() and "Second" in env.tg.last() and "Check medical" not in env.tg.last()


def test_overdue_in_progress_requires_note(env):
    tid = make_task(env, deadline="2026-09-25")
    env.db.set_status(tid, "progress", "A430")
    run(env.bot.run_checkin("17:00"))
    rid = env.bot.app.job_queue.once[0][1]["round"]
    press(env, "A430", f"ci:progress:{tid}:{rid}")
    assert "This note is required" in env.tg.last()
    type_text(env, "A430", "Carrier did not send the list yet")
    assert env.db.get_task(tid)["last_note"] == "Carrier did not send the list yet"


def test_no_employee_id_needed(env):
    # a new person who writes in the group becomes a member automatically
    new = SimpleNamespace(id=555, username="newguy", full_name="New Guy", is_bot=False)
    env.ctx[555] = SimpleNamespace(user_data={})
    upd = SimpleNamespace(effective_message=SimpleNamespace(text="hello", message_id=1), effective_user=new,
                          effective_chat=SimpleNamespace(id=GROUP, type="supergroup"), callback_query=None)
    run(env.bot.log_update(upd, env.ctx[555]))
    m = env.db.member_by_tg(555)
    assert m and m["name"] == "New Guy"
    # a seeded member is linked by Telegram username
    env.db.update_member("J125", "Joe", False, "joe_tg", True)
    joe = SimpleNamespace(id=777, username="Joe_TG", full_name="Joe", is_bot=False)
    assert env.bot.identify(joe)["id"] == "J125"


def test_private_chat_only_for_admins(env):
    type_text(env, "A430", "hello", chat_id=3, chat_type="private")
    assert "do not need a private chat" in env.tg.last()
    type_text(env, "N820", "hello", chat_id=1, chat_type="private")
    assert "Use the buttons" in env.tg.last()


def test_admin_flag_on_website_does_not_allow_approval(env):
    env.db.update_member("A430", "Ali", True, "ali", True)   # marked admin on the website
    tid = make_task(env)
    press(env, "A430", f"c:done:{tid}")
    type_text(env, "A430", f"approve {tid}")
    assert env.db.get_task(tid)["status"] == "approval"     # only Nusret / Abdulaziz accounts approve

# ---------------- website ----------------
def test_website_edits_stay_off_telegram(env):
    tid = make_task(env)
    before = env.db.get_task(tid)["updated_at"]
    web = create_web(env.cfg, env.db)
    c = web.test_client()
    assert c.get("/tasks").status_code == 302
    c.get("/login")
    with c.session_transaction() as s:
        token = s["csrf"]
    assert c.post("/login", data={"username": "boss", "password": "pw", "csrf": token}).status_code == 302
    assert b"Check medical cards" in c.get("/tasks").data
    with c.session_transaction() as s:
        token = s["csrf"]                                             # login gives a fresh token
    form = dict(csrf=token, title="Check medical cards NOW", description="List drivers", assignee="A430",
                deadline="2026-10-10", priority="urg", company="SEXTON GROUP LTD", category="🦺 Safety", notes="",
                status="progress", reason="")
    assert c.post(f"/tasks/{tid}", data=form).status_code == 302
    t = env.db.get_task(tid)
    assert t["title"] == "Check medical cards NOW" and t["status"] == "progress" and t["deadline"] == "2026-10-10"
    assert t["updated_at"] == before                                  # Telegram "last update" untouched
    assert all(h["source"] == "bot" for h in env.db.history(tid))     # Telegram history: no website rows
    assert len(env.db.history(tid, include_web=True)) > len(env.db.history(tid))
    bad = dict(form, company="lowercase llc")
    assert b"CAPITAL" in c.post(f"/tasks/{tid}", data=bad).data
    assert c.post(f"/tasks/{tid}", data=dict(form, csrf="wrong")).status_code == 400


# ---------------- website: login, pages, PDF, account ----------------
def web_login(env, user="boss", pw="pw"):
    c = create_web(env.cfg, env.db).test_client()
    c.get("/login")
    with c.session_transaction() as s:
        token = s["csrf"]
    r = c.post("/login", data={"username": user, "password": pw, "csrf": token})
    c.get("/health")                      # the next request creates a fresh form token
    with c.session_transaction() as s:
        token = s["csrf"]
    return c, r, token


def test_login_needs_username_and_password(env):
    _, r, _ = web_login(env, "boss", "wrong")
    assert b"Wrong username or password" in r.data
    _, r, _ = web_login(env, "someone", "pw")
    assert b"Wrong username or password" in r.data
    _, r, _ = web_login(env)
    assert r.status_code == 302


def test_every_page_renders(env):
    tid = make_task(env, title="Audit <script>x</script>")
    env.db.add_note(tid, "note", "A430")
    c, _, _ = web_login(env)
    for url in ["/", "/tasks", "/tasks?show=overdue&who=ALL&q=t-0001", "/tasks/new", f"/tasks/{tid}", "/history",
                "/history?period=month:2026-09&source=bot&q=audit", "/history?period=year:2026", "/reports",
                "/reports?kind=quarter&value=2026-Q3", "/reports?kind=year", "/members", "/account"]:
        r = c.get(url)
        assert r.status_code == 200, url
        assert b"<script>x</script>" not in r.data, url


def test_pdf_reports(env):
    tid = make_task(env, title="Проверка медкарт")        # Cyrillic must work in the PDF
    press(env, "A430", f"c:done:{tid}")
    type_text(env, "N820", f"approve {tid}")
    c, _, _ = web_login(env)
    for url in ["/reports/pdf?kind=month&value=2026-09&history=1", "/reports/pdf?kind=quarter&value=2026-Q3&history=0",
                "/reports/pdf?kind=year&value=2026", f"/tasks/{tid}/pdf"]:
        r = c.get(url)
        assert r.status_code == 200 and r.data[:4] == b"%PDF", url
    s = __import__("taskbot.report", fromlist=["x"]).summarize(env.db, "month", "2026-09")
    assert s["completed"] == 1 and s["created"] == 1
    assert c.get("/reports/pdf?kind=month&value=bad").status_code == 400


def test_account_change(env):
    c, _, token = web_login(env)
    r = c.post("/account", data={"csrf": token, "username": "algo_caser", "new_password": "NewStrongPass1",
                                 "again": "NewStrongPass1", "current": "pw"}, follow_redirects=True)
    assert b"Account saved" in r.data
    assert web_login(env, "boss", "pw")[1].status_code == 200            # old login refused
    assert web_login(env, "algo_caser", "NewStrongPass1")[1].status_code == 302


def test_faq_every_answer_opens(env):
    run(env.bot.cmd_faq(SimpleNamespace(effective_chat=SimpleNamespace(id=GROUP)), None))
    assert "Questions and answers" in env.tg.last()
    assert env.tg.buttons() == [f"q:{i}" for i in range(len(T.FAQ))]
    for i in range(len(T.FAQ)):
        text, rows = env.bot.faq_answer(i)
        assert "{" not in text and "}" not in text          # every placeholder filled
        assert rows[0][0] == ("⬅️ All questions", "q:list")
    assert "17:00 and 01:00" in env.bot.faq_answer(5)[0]
    assert "Nusret" in env.bot.faq_answer(12)[0] and "Abdulaziz" in env.bot.faq_answer(12)[0]
    edits = len(env.tg.edits)
    assert press(env, "S140", "q:3") == []                  # anyone can read the FAQ
    assert len(env.tg.edits) == edits + 1                   # answer replaces the same message
    assert "Start work" in env.tg.edits[-1][2]
