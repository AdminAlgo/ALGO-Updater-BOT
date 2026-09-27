"""The Telegram side: form, task cards, buttons, typed admin commands, check-ins."""
import asyncio
import logging
import re
import time
from datetime import date, time as dtime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import texts as T
from .db import OPEN

log = logging.getLogger(__name__)
HTML = ParseMode.HTML
ONCE = {"f", "dl", "co", "r", "e"}  # questions that close after one answer
ANSWER = {"progress": "🔵 Still in progress", "block": "⏸ Blocked", "done": "✅ Done", "reject": "⛔ Reject"}
EDIT_FIELDS = [s for s in T.STEPS]


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def keyboard(rows):
    """rows of (label, callback) or (label, 'url:https://...')."""
    if not rows:
        return None
    out = []
    for row in rows:
        btns = []
        for label, data in row:
            if data.startswith("url:"):
                btns.append(InlineKeyboardButton(label, url=data[4:]))
            else:
                btns.append(InlineKeyboardButton(label, callback_data=data))
        out.append(btns)
    return InlineKeyboardMarkup(out)


class TaskBot:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.app = None
        self.loop = None
        self.prompts = {}       # (mode, task_id) -> (chat_id, message_id) of an open reason question
        self.approval_msgs = {}  # task_id -> (chat_id, message_id) of the "please approve" message

    # ================= setup =================
    def build(self):
        app = Application.builder().token(self.cfg.token).post_init(self._post_init).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("newtask", self.cmd_newtask))
        app.add_handler(CommandHandler("tasks", self.cmd_tasks))
        app.add_handler(CommandHandler("my", self.cmd_my))
        app.add_handler(CommandHandler("overdue", self.cmd_overdue))
        app.add_handler(CommandHandler("report", self.cmd_report))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("setgroup", self.cmd_setgroup))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        jq = app.job_queue
        for hh, mm in self.cfg.checkin_times:
            jq.run_daily(self.job_checkin, time=dtime(hh, mm, tzinfo=self.cfg.tz), data=f"{hh:02d}:{mm:02d}",
                         name=f"checkin {hh:02d}:{mm:02d}")
        every = timedelta(hours=self.cfg.overdue_every_hours)
        jq.run_repeating(self.job_overdue, interval=every, first=every, name="overdue")
        self.app = app
        return app

    async def _post_init(self, app):
        self.loop = asyncio.get_running_loop()
        await app.bot.set_my_commands([
            ("newtask", "Create a new task"), ("tasks", "Task list"), ("my", "My tasks"),
            ("overdue", "Overdue tasks"), ("report", "Report"), ("help", "How it works"), ("cancel", "Stop the form"),
        ])
        log.info("Bot @%s ready. Group: %s", app.bot.username, self.group_id())

    async def on_error(self, update, context):
        log.exception("Error while handling an update", exc_info=context.error)

    # ================= helpers =================
    def members(self):
        return {m["id"]: m for m in self.db.members(active_only=False)}

    def group_id(self):
        v = self.db.get_setting("group_chat_id")
        return int(v) if v else self.cfg.group_chat_id

    def is_admin(self, member, user=None):
        if member and member["is_admin"]:
            return True
        return bool(user and (user.username or "").lower() in self.cfg.admin_usernames)

    def can_work(self, member, user, task):
        return (self.is_admin(member, user) or task["assignee"] == T.ALL
                or member["id"] in (task["assignee"], task["creator"]))

    def today(self):
        return self.db.today()

    def now_label(self):
        return T.fmt_stamp(self.db.stamp())

    async def send(self, chat_id, text, rows=None, reply_to=None):
        return await self.app.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=keyboard(rows),
                                               disable_web_page_preview=True, reply_to_message_id=reply_to)

    async def announce(self, text, rows=None):
        gid = self.group_id()
        if not gid:
            log.warning("No team group set; skipped: %s", text[:60])
            return None
        try:
            return await self.send(gid, text, rows)
        except TelegramError:
            log.exception("Could not post to the team group")
            return None

    async def edit(self, chat_id, message_id, text, rows=None):
        try:
            await self.app.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=HTML,
                                                 reply_markup=keyboard(rows), disable_web_page_preview=True)
            return True
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return True
            log.info("Edit failed for %s/%s: %s", chat_id, message_id, e)
            return False
        except Forbidden:
            return False

    async def strip_kb(self, chat_id, message_id):
        try:
            await self.app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramError:
            pass

    async def close_prompt(self, message, label, who_name):
        if not isinstance(message, Message):
            return
        text = (message.text_html or "") + f"\n\n✔️ <b>{T.h(label)}</b> · {T.h(who_name)}"
        await self.edit(message.chat_id, message.message_id, text)

    def bot_link(self, payload="newtask"):
        return f"url:https://t.me/{self.app.bot.username}?start={payload}"

    # ================= task cards =================
    def card_rows(self, t):
        i = t["id"]

        def b(label, verb):
            return (label, f"c:{verb}:{i}")

        tools = [b("✏️ Edit", "edit"), b("📝 Note", "note"), b("📜 History", "hist")]
        st = t["status"]
        if st == "new":
            return [[b("▶️ Start work", "start"), b("⏸ Blocked", "block")], [b("✅ Done", "done"), b("⛔ Reject", "reject")], tools]
        if st == "progress":
            return [[b("⏸ Blocked", "block"), b("✅ Done", "done")], [b("⛔ Reject", "reject")], tools]
        if st == "blocked":
            return [[b("▶️ Resume work", "start"), b("✅ Done", "done")], [b("⛔ Reject", "reject")], tools]
        if st == "approval":
            return [[b("✅ Approve", "approve"), b("↩️ Not completed yet", "back")], [b("✏️ Edit", "edit"), b("📜 History", "hist")]]
        return [[b("♻️ Reopen", "reopen"), b("📜 History", "hist")]]

    async def post_card(self, chat_id, task_id, intro=None):
        t = self.db.get_task(task_id)
        msg = await self.send(chat_id, T.card(t, self.members(), self.today(), intro), self.card_rows(t))
        self.db.add_card(task_id, chat_id, msg.message_id)
        return msg

    async def refresh_cards(self, task_id):
        t = self.db.get_task(task_id)
        if not t:
            return
        text, rows = T.card(t, self.members(), self.today()), self.card_rows(t)
        for r in self.db.cards(task_id):
            if not await self.edit(r["chat_id"], r["message_id"], text, rows):
                self.db.drop_card(r["chat_id"], r["message_id"])

    async def change_status(self, t, status, member, reason=None, extra=""):
        note = f"Status: {T.STATUS[t['status']]} → {T.STATUS[status]}" + (f" · {extra}" if extra else "")
        self.db.set_status(t["id"], status, member["id"], "bot", reason=reason, history_text=note)
        await self.refresh_cards(t["id"])

    # ---- calls from the website thread ----
    def notify_refresh(self, task_id):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.refresh_cards(task_id), self.loop)

    def notify_new_task(self, task_id):
        async def go():
            gid = self.group_id()
            if gid:
                t = self.db.get_task(task_id)
                members = self.members()
                intro = f"🆕 <b>New task</b>\n{T.mention(members, t['assignee'])}, this one is for you."
                await self.post_card(gid, task_id, intro)
        if self.loop:
            asyncio.run_coroutine_threadsafe(go(), self.loop)

    # ================= commands =================
    def menu_rows(self):
        return [[("➕ New task", "m:new"), ("📋 Task list", "l:all")],
                [("👤 My tasks", "m:mine"), ("🔴 Overdue", "l:over")],
                [("🔍 Find by ID", "m:find"), ("📊 Report", "m:report")],
                [("❓ Help", "m:help")]]

    async def need_member(self, update, context):
        member = self.db.member_by_tg(update.effective_user.id)
        if member:
            return member
        chat = update.effective_chat
        if chat.type == ChatType.PRIVATE:
            await self.send(chat.id, "👋 Please send your <b>employee ID</b> first. Example: N820")
        else:
            await self.send(chat.id, "👋 Open the bot in private and send your employee ID first.",
                            [[("🤖 Open the bot", self.bot_link("register"))]], reply_to=update.effective_message.message_id)
        return None

    async def cmd_start(self, update, context):
        chat = update.effective_chat
        if chat.type != ChatType.PRIVATE:
            set_up = self.group_id() == chat.id
            await self.send(chat.id,
                            "👋 <b>I am ALGO Task Bot.</b>\n"
                            + ("This group is already the team task group ✅\n" if set_up else
                               "1) Make me an <b>admin</b> of this group.\n2) An admin sends /setgroup here.\n")
                            + "Everyone: open me in private and send your employee ID once.",
                            [[("🤖 Open the bot", self.bot_link("register"))]], reply_to=update.effective_message.message_id)
            return
        payload = context.args[0] if context.args else ""
        member = self.db.member_by_tg(update.effective_user.id)
        if not member:
            context.user_data["after_register"] = payload
            await self.send(chat.id, "👋 <b>Welcome to ALGO Task Bot!</b>\nSend your employee ID to register. Example: N820")
            return
        if payload == "newtask":
            return await self.start_form(chat.id, context)
        await self.send(chat.id, f"👋 Hi {T.h(member['name'])}! What do you want to do?", self.menu_rows())

    async def cmd_newtask(self, update, context):
        chat = update.effective_chat
        if chat.type != ChatType.PRIVATE:
            await self.send(chat.id, "📝 The form opens in your private chat with me 👇",
                            [[("➕ Open the form", self.bot_link())]], reply_to=update.effective_message.message_id)
            return
        if await self.need_member(update, context):
            await self.start_form(chat.id, context)

    async def cmd_tasks(self, update, context):
        await self.send(update.effective_chat.id, T.task_list(self.db.tasks(), self.members(), self.today(), self.now_label()),
                        self.list_rows())

    async def cmd_my(self, update, context):
        member = await self.need_member(update, context)
        if member:
            await self.show_simple_list(update.effective_chat.id, "person", member["id"])

    async def cmd_overdue(self, update, context):
        await self.show_simple_list(update.effective_chat.id, "over")

    async def cmd_report(self, update, context):
        await self.send(update.effective_chat.id, self.report_text())

    async def cmd_help(self, update, context):
        await self.send(update.effective_chat.id, self.help_text())

    async def cmd_cancel(self, update, context):
        for k in ("form", "pending", "dl", "co"):
            context.user_data.pop(k, None)
        await self.send(update.effective_chat.id, "❌ Stopped. Nothing was saved.")

    async def cmd_setgroup(self, update, context):
        chat, user = update.effective_chat, update.effective_user
        if chat.type == ChatType.PRIVATE:
            await self.send(chat.id, "Send /setgroup inside the team group, not here.")
            return
        member = self.db.member_by_tg(user.id)
        if not self.is_admin(member, user):
            await self.send(chat.id, "⛔ Only admins can set the team group.", reply_to=update.effective_message.message_id)
            return
        self.db.set_setting("group_chat_id", chat.id)
        await self.send(chat.id, "✅ This group is now the <b>team task group</b>.")
        msg = await self.send(chat.id, self.welcome_text(), [[("➕ New task", self.bot_link()), ("📋 Task list", "l:all")]])
        try:
            await self.app.bot.pin_chat_message(chat.id, msg.message_id, disable_notification=True)
        except TelegramError:
            await self.send(chat.id, "ℹ️ Make me an admin of this group so I can pin this message and read typed commands.")

    def welcome_text(self):
        times = " and ".join(f"{h:02d}:{m:02d}" for h, m in self.cfg.checkin_times)
        return ("👋 <b>Hi team! I am ALGO Task Bot.</b>\nI keep track of our tasks and deadlines.\n\n"
                "➕ <b>New task</b>: add a task with a short form.\n📋 <b>Task list</b>: see all tasks.\n"
                f"At {times} I ask about every open task.\n\n"
                "First time? Open me in private and send your employee ID.")

    def help_text(self):
        times = " and ".join(f"{h:02d}:{m:02d}" for h, m in self.cfg.checkin_times)
        return T.HELP.format(times=times, remind=self.cfg.remind_after_min, escalate=self.cfg.escalate_after_min,
                             overdue=self.cfg.overdue_every_hours)

    def report_text(self):
        tasks = self.db.tasks()
        c = lambda s: sum(1 for t in tasks if t["status"] == s)
        over = sum(1 for t in tasks if T.is_overdue(t, self.today()))
        return (f"📊 <b>Report · {self.now_label()}</b>\n\n🆕 New: {c('new')}\n🔵 In progress: {c('progress')}\n"
                f"⏸ Waiting / Blocked: {c('blocked')}\n🟣 Needs approval: {c('approval')}\n✅ Done: {c('done')}\n"
                f"⛔ Rejected: {c('rejected')}\n\n🔴 Overdue: {over}")

    # ================= typed text =================
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not msg or not user:
            return
        text = (msg.text or "").strip()
        member = self.db.member_by_tg(user.id)
        private = chat.type == ChatType.PRIVATE

        if not member:
            if private:
                await self.register(chat.id, user, text, context)
            return

        cmd = T.parse_admin_command(text)
        if cmd:
            kind, task_id = cmd
            t = self.db.get_task(task_id)

            async def reply(s):
                await self.send(chat.id, s, reply_to=msg.message_id)
            if not t:
                return await reply(f"🔍 There is no task <b>{T.code(task_id)}</b>.")
            if kind == "approve":
                return await self.approve(t, member, user, reply)
            return await self.not_done(t, member, user, reply)

        p = context.user_data.get("pending")
        if p and p["chat_id"] == chat.id:
            return await self.answer_pending(p, text, chat.id, context, member, user)
        if private:
            await self.send(chat.id, "Use the buttons below 👇", self.menu_rows())

    async def register(self, chat_id, user, text, context):
        if not T.looks_like_employee_id(text):
            await self.send(chat_id, "👋 Please send your <b>employee ID</b>. Example: N820")
            return
        result = self.db.link_member(text.upper(), user.id, user.username or "")
        if result == "unknown":
            await self.send(chat_id, f"❓ I do not know the ID <b>{T.h(text.upper())}</b>. Check it, or ask an admin to add you.")
        elif result == "taken":
            await self.send(chat_id, "⛔ This ID is already linked to another Telegram account. Ask an admin to help.")
        elif result == "inactive":
            await self.send(chat_id, "⛔ This ID is switched off. Ask an admin to help.")
        else:
            m = self.db.member_by_tg(user.id)
            role = "Admin" if self.is_admin(m, user) else "Member"
            await self.send(chat_id, f"✅ <b>Confirmed.</b> You are <b>{T.h(m['name'])} · {role}</b>.\nWhat do you want to do?",
                            self.menu_rows())
            if context.user_data.pop("after_register", "") == "newtask":
                await self.start_form(chat_id, context)

    async def answer_pending(self, p, text, chat_id, context, member, user):
        if p.get("msg_id"):
            await self.strip_kb(chat_id, p["msg_id"])
        key = p.get("key")
        if key == "company" and p["kind"] in ("form", "edit"):
            err = T.company_error(text)
            if err:
                up = text.upper()
                has_letters = any(c.isalpha() for c in text)
                warn = f"⚠️ <b>{err}</b>\nYou wrote: {T.h(text)}"
                if has_letters:
                    warn += f"\n\nPress the button to use <b>{T.h(up)}</b>, or type it again."
                    context.user_data["co"] = {"value": up, "pending": p}
                m = await self.send(chat_id, warn, [[(f"✅ Use: {up}"[:60], "co:use")]] if has_letters else None)
                p["msg_id"] = m.message_id
                return
        context.user_data.pop("pending", None)
        context.user_data.pop("co", None)
        await self.resolve(p, text, chat_id, context, member, user)

    async def resolve(self, p, value, chat_id, context, member, user):
        kind = p["kind"]
        if kind == "form":
            form = context.user_data.get("form")
            if not form:
                return
            form["data"][p["key"]] = value.strip()
            await self.next_step(chat_id, context)
            return
        if kind == "find":
            n = T.parse_task_ref(value)
            if n and self.db.get_task(n):
                await self.post_card(chat_id, n)
            else:
                await self.send(chat_id, f"🔍 Nothing found for <b>{T.h(value)}</b>. Check the number and try again.")
            return
        t = self.db.get_task(p["task_id"])
        if not t:
            return
        if kind == "reason":
            await self.apply_reason(p["mode"], t, value.strip(), member)
        elif kind == "note":
            await self.add_note(t, value.strip(), member)
        elif kind == "edit":
            await self.apply_edit(t, p["key"], value.strip(), member)

    # ================= buttons =================
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        if data == "noop":
            await q.answer()
            return
        member = self.db.member_by_tg(q.from_user.id)
        if not member:
            await q.answer("Open the bot in private and send your employee ID first.", show_alert=True)
            return
        parts = data.split(":")
        handler = {
            "m": self.cb_menu, "l": self.cb_list, "f": self.cb_form, "dl": self.cb_deadline, "co": self.cb_company,
            "c": self.cb_card, "r": self.cb_reason, "e": self.cb_edit, "ci": self.cb_checkin,
        }.get(parts[0])
        if not handler:
            await q.answer()
            return
        result = await handler(q, context, member, parts)
        if result is False:
            return  # the handler already answered with a warning
        if parts[0] in ONCE and isinstance(q.message, Message):
            await self.close_prompt(q.message, result if isinstance(result, str) else self.pressed_label(q), member["name"])
        try:
            await q.answer()
        except TelegramError:
            pass

    @staticmethod
    def pressed_label(q):
        markup = q.message.reply_markup if isinstance(q.message, Message) else None
        for row in (markup.inline_keyboard if markup else []):
            for b in row:
                if b.callback_data == q.data:
                    return b.text
        return "OK"

    async def alert(self, q, text):
        await q.answer(text[:190], show_alert=True)
        return False

    def task_of(self, parts, index=2):
        try:
            return self.db.get_task(int(parts[index]))
        except (IndexError, ValueError):
            return None

    # ---------- menu & lists ----------
    async def cb_menu(self, q, context, member, parts):
        chat_id, v = q.message.chat_id, parts[1]
        if v == "new":
            if q.message.chat.type != ChatType.PRIVATE:
                return await self.alert(q, "Use ➕ New task in the pinned message. The form opens in private chat.")
            await self.start_form(chat_id, context)
        elif v == "mine":
            await self.show_simple_list(chat_id, "person", member["id"])
        elif v == "find":
            await self.send(chat_id, "🔍 Send the task number. Example: <b>T-0038</b> or just <b>38</b>.")
            context.user_data["pending"] = {"kind": "find", "chat_id": chat_id, "msg_id": None}
        elif v == "report":
            await self.send(chat_id, self.report_text())
        elif v == "help":
            await self.send(chat_id, self.help_text())

    def list_rows(self):
        return [[("🔴 Overdue", "l:over"), ("🟣 Needs approval", "l:appr")],
                [("👤 By person", "l:people"), ("✅ Done / ⛔ Rejected", "l:done")],
                [("🔎 Open a task", "l:pick"), ("📋 All tasks", "l:all")]]

    async def show_simple_list(self, chat_id, kind, arg=None):
        tasks, members, today = self.db.tasks(), self.members(), self.today()
        if kind == "over":
            title, rows, empty = "🔴 Overdue tasks", [t for t in tasks if T.is_overdue(t, today)], "✅ Nothing is overdue."
        elif kind == "appr":
            title, rows, empty = "🟣 Waiting for approval", [t for t in tasks if t["status"] == "approval"], "Nothing is waiting for approval."
        elif kind == "done":
            title, rows, empty = "✅ Done and ⛔ Rejected", [t for t in tasks if t["status"] in ("done", "rejected")][-30:], "No closed tasks yet."
        else:
            title = "👤 Tasks of " + T.name_of(members, arg)
            rows = [t for t in tasks if t["assignee"] == arg and t["status"] not in ("done", "rejected")]
            empty = f"🎉 {T.name_of(members, arg)} has no open tasks."
        if not rows:
            await self.send(chat_id, empty)
            return
        text = f"<b>{title} ({len(rows)})</b>\n" + "\n".join(T.STATUS_ICON[t["status"]] + " " + T.list_line(t, members)[2:] for t in rows)
        await self.send(chat_id, text, [[(f"👀 Open #{T.code(t['id'])}", f"c:show:{t['id']}")] for t in rows[:20]])

    async def cb_list(self, q, context, member, parts):
        chat_id, v = q.message.chat_id, parts[1]
        if v == "all":
            await self.send(chat_id, T.task_list(self.db.tasks(), self.members(), self.today(), self.now_label()), self.list_rows())
        elif v in ("over", "appr", "done"):
            await self.show_simple_list(chat_id, v)
        elif v == "people":
            people = [(m["name"], f"l:person:{m['id']}") for m in self.db.members()]
            await self.send(chat_id, "👤 Whose tasks do you want to see?", chunk(people, 3) + [[("👥 All team tasks", f"l:person:{T.ALL}")]])
        elif v == "person":
            await self.show_simple_list(chat_id, "person", parts[2])
        elif v == "pick":
            open_tasks = [t for t in self.db.tasks() if t["status"] not in ("done", "rejected")]
            if not open_tasks:
                await self.send(chat_id, "🎉 No open tasks.")
            else:
                await self.send(chat_id, "🔎 Which task do you want to open?",
                                [[(f"{T.STATUS_ICON[t['status']]} #{T.code(t['id'])} · {T.shorten(t['title'], 28)}", f"c:show:{t['id']}")]
                                 for t in open_tasks[:30]])

    # ================= the form =================
    def options(self, key):
        """(label, token) pairs for a button field."""
        if key == "assignee":
            return [(f"{m['name']} · {m['id']}", m["id"]) for m in self.db.members()] + [("👥 All team members", T.ALL)]
        if key == "priority":
            return [(label, k) for k, label in T.PRIORITY.items()]
        if key == "category":
            return [(c, str(i)) for i, c in enumerate(T.CATEGORIES)]
        return []

    def option_value(self, key, token):
        if key == "assignee":
            return token if token == T.ALL or self.db.get_member(token) else None
        if key == "priority":
            return token if token in T.PRIORITY else None
        if key == "category":
            return T.CATEGORIES[int(token)] if token.isdigit() and int(token) < len(T.CATEGORIES) else None
        return None

    def option_rows(self, key, prefix):
        opts = [(label, prefix + token) for label, token in self.options(key)]
        if key == "assignee":
            return chunk(opts[:-1], 3) + [[opts[-1]]]
        return chunk(opts, 2)

    def step_head(self, i):
        st = T.STEPS[i]
        return f"📝 <b>Step {i + 1} of {len(T.STEPS)} · {st['icon']} {st['label']}</b>\n{st['q']}"

    def form_nav(self, form):
        nav = []
        if form["step"] > 0 and not form["from_review"]:
            nav.append(("⬅️ Back", f"f:back:{form['step']}"))
        nav.append(("❌ Cancel", "f:cancel"))
        return nav

    async def start_form(self, chat_id, context):
        for k in ("pending", "dl", "co"):
            context.user_data.pop(k, None)
        context.user_data["form"] = {"step": 0, "data": {}, "from_review": False}
        await self.send(chat_id, f"📝 <b>New task form</b>\nAnswer {len(T.STEPS)} short questions. "
                                 "You can go back or change anything before it is saved.")
        await self.ask_step(chat_id, context)

    async def ask_step(self, chat_id, context):
        form = context.user_data["form"]
        i = form["step"]
        st = T.STEPS[i]
        if st["key"] == "deadline":
            return await self.deadline_start(chat_id, context, {"mode": "form"})
        if st.get("text"):
            rows = [[("⏭ Skip", f"f:skip:{i}")]] if st.get("skip") else []
            rows.append(self.form_nav(form))
            m = await self.send(chat_id, self.step_head(i), rows)
            context.user_data["pending"] = {"kind": "form", "key": st["key"], "chat_id": chat_id, "msg_id": m.message_id}
        else:
            rows = self.option_rows(st["key"], f"f:pick:{i}:") + [self.form_nav(form)]
            await self.send(chat_id, self.step_head(i), rows)

    async def next_step(self, chat_id, context):
        form = context.user_data["form"]
        if form["from_review"]:
            return await self.review(chat_id, context)
        form["step"] += 1
        if form["step"] >= len(T.STEPS):
            return await self.review(chat_id, context)
        await self.ask_step(chat_id, context)

    async def review(self, chat_id, context):
        form = context.user_data["form"]
        form["from_review"] = False
        context.user_data.pop("pending", None)
        members = self.members()
        lines = [f"{s['icon']} {s['label']}: <b>{T.h(T.field_value(members, s['key'], form['data'].get(s['key'], '')))}</b>"
                 for s in T.STEPS]
        await self.send(chat_id, "🧾 <b>Check the task before saving</b>\n\n" + "\n".join(lines) + "\n\nIs everything correct?",
                        [[("✅ Create task", "f:create")], [("✏️ Change a field", "f:change"), ("❌ Cancel", "f:cancel")]])

    async def cb_form(self, q, context, member, parts):
        form = context.user_data.get("form")
        if not form:
            return await self.alert(q, "This form is closed. Press ➕ New task to start again.")
        chat_id, v = q.message.chat_id, parts[1]
        if v in ("pick", "skip", "back") and int(parts[2]) != form["step"]:
            return await self.alert(q, "This question is old. Answer the newest message below.")
        context.user_data.pop("pending", None)
        if v == "pick":
            key = T.STEPS[form["step"]]["key"]
            value = self.option_value(key, parts[3])
            if value is None:
                return await self.alert(q, "This choice is not available any more. Pick again.")
            form["data"][key] = value
            await self.next_step(chat_id, context)
            return T.field_value(self.members(), key, value)
        if v == "skip":
            form["data"]["notes"] = ""
            await self.next_step(chat_id, context)
            return "Skipped"
        if v == "back":
            form["step"] = max(0, form["step"] - 1)
            await self.ask_step(chat_id, context)
            return "Back"
        if v == "cancel":
            for k in ("form", "dl", "co"):
                context.user_data.pop(k, None)
            await self.send(chat_id, "❌ Cancelled. Nothing was saved.", [[("➕ Start again", "m:new")]])
            return "Cancel"
        if v == "change":
            rows = chunk([(f"{s['icon']} {s['label']}", f"f:goto:{i}") for i, s in enumerate(T.STEPS)], 2)
            await self.send(chat_id, "✏️ Which field do you want to change?", rows)
            return "Change a field"
        if v == "goto":
            form["step"], form["from_review"] = int(parts[2]), True
            await self.ask_step(chat_id, context)
            return T.STEPS[int(parts[2])]["label"]
        if v == "create":
            d = form["data"]
            missing = [s["label"] for s in T.STEPS if not s.get("skip") and not d.get(s["key"])]
            if missing:
                return await self.alert(q, "Please fill: " + ", ".join(missing))
            task_id = self.db.create_task(d, member["id"], "bot")
            context.user_data.pop("form", None)
            members = self.members()
            gid = self.group_id()
            if gid:
                t = self.db.get_task(task_id)
                intro = f"🆕 <b>New task</b> from {T.mention(members, member['id'])}\n{T.mention(members, t['assignee'])}, this one is for you."
                await self.post_card(gid, task_id, intro)
                await self.send(chat_id, f"✅ Task <b>#{T.code(task_id)}</b> is saved and posted in the team group.",
                                [[("➕ Add another task", "m:new")]])
            else:
                await self.send(chat_id, f"✅ Task <b>#{T.code(task_id)}</b> is saved.\n⚠️ No team group is set yet. "
                                         "An admin must send /setgroup inside the group.")
                await self.post_card(chat_id, task_id)
            return f"Created #{T.code(task_id)}"

    # ---------- deadline: month -> day -> confirm ----------
    async def deadline_start(self, chat_id, context, ctx):
        ctx["chat_id"] = chat_id
        context.user_data["dl"] = ctx
        if ctx["mode"] == "form":
            head = self.step_head(context.user_data["form"]["step"])
            nav = self.form_nav(context.user_data["form"])
        else:
            t = self.db.get_task(ctx["task_id"])
            head = f"⏰ <b>New deadline for #{T.code(t['id'])}</b>\nNow: {T.fmt_date(t['deadline'])}"
            nav = [("✖️ Cancel", "e:close")]
        today = self.db.now().date()
        months = []
        for i in range(6):
            y, m = today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1
            months.append((f"{T.MON[m - 1]} {y}", f"dl:m:{y}-{m:02d}"))
        await self.send(chat_id, head + "\n\n<b>1) Choose the month</b>", chunk(months, 3) + [nav])

    async def cb_deadline(self, q, context, member, parts):
        ctx = context.user_data.get("dl")
        if not ctx:
            return await self.alert(q, "This question is closed.")
        chat_id, v = q.message.chat_id, parts[1]
        today = self.today()
        if v == "m":
            y, m = map(int, parts[2].split("-"))
            first = date(y, m, 1)
            days = ((date(y + (m == 12), m % 12 + 1, 1)) - first).days
            cells = [(" ", "noop")] * first.weekday()
            for d in range(1, days + 1):
                iso = f"{y}-{m:02d}-{d:02d}"
                cells.append(("·", "noop") if iso < today else (str(d), f"dl:d:{iso}"))
            while len(cells) % 7:
                cells.append((" ", "noop"))
            rows = [[(x, "noop") for x in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")]] + chunk(cells, 7)
            rows.append([("⬅️ Change month", "dl:back")])
            await self.send(chat_id, f"<b>2) Choose the day</b> · {T.MONTH_FULL[m - 1]} {y}\n<i>Days with a dot · have already passed.</i>", rows)
            return f"{T.MONTH_FULL[m - 1]} {y}"
        if v in ("back", "again"):
            await self.deadline_start(chat_id, context, ctx)
            return "Change month" if v == "back" else "Choose again"
        if v == "d":
            iso = parts[2]
            await self.send(chat_id, f"<b>3) Confirm the deadline</b>\n⏰ <b>{T.fmt_date(iso)}</b>\n\nIs this correct?",
                            [[("✅ Confirm", f"dl:ok:{iso}")], [("🔁 Choose again", "dl:again")]])
            return T.fmt_date(iso)
        if v == "ok":
            iso = parts[2]
            if iso < today:
                return await self.alert(q, "This day has already passed. Choose again.")
            context.user_data.pop("dl", None)
            if ctx["mode"] == "form":
                context.user_data["form"]["data"]["deadline"] = iso
                await self.next_step(chat_id, context)
            else:
                t = self.db.get_task(ctx["task_id"])
                if not self.can_work(member, q.from_user, t):
                    return await self.alert(q, "⛔ You cannot edit this task.")
                await self.apply_edit(t, "deadline", iso, member)
            return "Confirmed: " + T.fmt_date(iso)

    async def cb_company(self, q, context, member, parts):
        co = context.user_data.pop("co", None)
        if not co:
            return await self.alert(q, "This question is closed.")
        p = co["pending"]
        context.user_data.pop("pending", None)
        await self.resolve(p, co["value"], q.message.chat_id, context, member, q.from_user)
        return "Use: " + co["value"]

    # ================= task buttons =================
    async def cb_card(self, q, context, member, parts):
        verb, t = parts[1], self.task_of(parts)
        if not t:
            return await self.alert(q, "This task does not exist any more.")
        chat_id, user = q.message.chat_id, q.from_user
        if verb == "hist":
            await self.send(chat_id, T.history_text(t, self.db.history(t["id"]), self.members()))
            return
        if verb == "show":
            await self.post_card(chat_id, t["id"])
            return
        if verb == "approve":
            return await self.approve(t, member, user, lambda s: self.alert(q, strip_tags(s)))
        if verb == "back":
            return await self.not_done(t, member, user, lambda s: self.alert(q, strip_tags(s)))
        if verb == "reopen":
            if not self.is_admin(member, user):
                return await self.alert(q, f"⛔ Only {T.admin_names(self.members())} can reopen tasks.")
            await self.change_status(t, "progress", member, extra="reopened")
            members = self.members()
            await self.announce(f"♻️ {T.mention(members, member['id'])} reopened <b>#{T.code(t['id'])}</b>\n"
                                f"{T.mention(members, t['assignee'])}, please continue.")
            return
        if not self.can_work(member, user, t):
            return await self.alert(q, f"⛔ Only the creator, the assignee ({T.name_of(self.members(), t['assignee'])}) "
                                       "or an admin can change this task.")
        if verb == "start":
            if t["status"] not in OPEN:
                return await self.alert(q, "Already " + T.STATUS[t["status"]])
            await self.change_status(t, "progress", member)
            await self.announce(f"▶️ {T.mention(self.members(), member['id'])} started work on <b>#{T.code(t['id'])}</b>\n{T.h(t['title'])}")
        elif verb in ("block", "reject"):
            r = await self.ask_reason(q, verb, t, chat_id)
            if r is False:
                return False
        elif verb == "done":
            r = await self.do_done(q, t, member)
            if r is False:
                return False
        elif verb == "edit":
            rows = chunk([(f"{s['icon']} {s['label']}", f"e:field:{t['id']}:{s['key']}") for s in EDIT_FIELDS], 2)
            await self.send(chat_id, f"✏️ <b>Edit #{T.code(t['id'])}</b>\nWhat do you want to change?", rows + [[("✖️ Close", "e:close")]])
        elif verb == "note":
            await self.send(chat_id, f"📝 Send a note for <b>#{T.code(t['id'])}</b>. Everyone will see it and it is saved in the history.")
            context.user_data["pending"] = {"kind": "note", "task_id": t["id"], "chat_id": chat_id, "msg_id": None}

    async def do_done(self, q, t, member):
        if t["status"] not in OPEN:
            return await self.alert(q, "This task is already " + T.STATUS[t["status"]])
        await self.change_status(t, "approval", member)
        members = self.members()
        c = T.code(t["id"])
        m = await self.announce(
            f"🟣 {T.mention(members, member['id'])} marked <b>#{c}</b> as done:\n{T.h(t['title'])}\n\n"
            f"{T.admins_mention(members)} please check. From your own account type:\n"
            f"<code>approve {c}</code> to close it\n<code>not completed yet {c}</code> to send it back",
            [[("✅ Approve", f"c:approve:{t['id']}"), ("↩️ Not completed yet", f"c:back:{t['id']}")]])
        if m:
            self.approval_msgs[t["id"]] = (m.chat_id, m.message_id)

    async def close_approval(self, task_id):
        ref = self.approval_msgs.pop(task_id, None)
        if ref:
            try:
                await self.app.bot.edit_message_reply_markup(chat_id=ref[0], message_id=ref[1], reply_markup=None)
            except TelegramError:
                pass

    async def approve(self, t, member, user, reply):
        members = self.members()
        if not self.is_admin(member, user):
            await reply(f"⛔ Only {T.admin_names(members)} can approve tasks, from their own account.")
            return False
        if t["status"] != "approval":
            await reply(f"ℹ️ <b>#{T.code(t['id'])}</b> is not waiting for approval. Now: {T.STATUS[t['status']]}")
            return False
        await self.change_status(t, "done", member, extra="approved")
        await self.close_approval(t["id"])
        done_by = t["done_by"] or t["assignee"]
        await self.announce(f"✅ <b>#{T.code(t['id'])}</b> approved by {T.mention(members, member['id'])}. Task closed.\n"
                            f"{T.h(t['title'])}\nCompleted by {T.mention(members, done_by)} 👏")

    async def not_done(self, t, member, user, reply):
        members = self.members()
        if not self.is_admin(member, user):
            await reply(f"⛔ Only {T.admin_names(members)} can do this, from their own account.")
            return False
        if t["status"] != "approval":
            await reply(f"ℹ️ <b>#{T.code(t['id'])}</b> is not waiting for approval. Now: {T.STATUS[t['status']]}")
            return False
        await self.change_status(t, "progress", member, extra="not completed yet")
        await self.close_approval(t["id"])
        await self.announce(f"↩️ {T.mention(members, member['id'])}: <b>#{T.code(t['id'])}</b> is <b>not completed yet</b>.\n"
                            f"{T.h(t['title'])}\n{T.mention(members, t['assignee'])}, it needs to be completed.")

    # ---------- reasons ----------
    async def ask_reason(self, q, mode, t, chat_id):
        if mode == "block" and t["status"] == "blocked":
            return await self.alert(q, "Already waiting. Press ▶️ Resume work first, or add a note.")
        if t["status"] not in OPEN:
            return await self.alert(q, "This task is already " + T.STATUS[t["status"]])
        opened = self.prompts.get((mode, t["id"]))
        if opened and time.monotonic() - opened[2] < 1800:
            return await self.alert(q, "This question is already open below 👇 Pick a reason there.")
        reasons = T.BLOCK_REASONS if mode == "block" else T.REJECT_REASONS
        c = T.code(t["id"])
        head = (f"⏸ Why is <b>#{c}</b> waiting? Pick a reason or type your own." if mode == "block"
                else f"⛔ Why reject <b>#{c}</b>? Pick a reason or type your own.")
        rows = [[(r, f"r:{mode}:{t['id']}:{i}")] for i, r in enumerate(reasons)]
        rows.append([("✍️ Type my own", f"r:{mode}:{t['id']}:type"), ("✖️ Cancel", f"r:{mode}:{t['id']}:cancel")])
        m = await self.send(chat_id, head, rows)
        self.prompts[(mode, t["id"])] = (chat_id, m.message_id, time.monotonic())

    async def cb_reason(self, q, context, member, parts):
        mode, t, x = parts[1], self.task_of(parts), parts[3]
        if not t:
            return await self.alert(q, "This task does not exist any more.")
        if not self.can_work(member, q.from_user, t):
            return await self.alert(q, "⛔ You cannot change this task.")
        self.prompts.pop((mode, t["id"]), None)
        if x == "cancel":
            return "Cancelled. Nothing changed."
        if x == "type":
            await self.send(q.message.chat_id, "✍️ Send the reason as a message 👇")
            context.user_data["pending"] = {"kind": "reason", "mode": mode, "task_id": t["id"], "chat_id": q.message.chat_id, "msg_id": None}
            return "Type my own"
        reason = (T.BLOCK_REASONS if mode == "block" else T.REJECT_REASONS)[int(x)]
        await self.apply_reason(mode, t, reason, member)
        return reason

    async def apply_reason(self, mode, t, reason, member):
        t = self.db.get_task(t["id"])
        if t["status"] not in OPEN:
            return
        members = self.members()
        c = T.code(t["id"])
        if mode == "block":
            await self.change_status(t, "blocked", member, reason=reason, extra="reason: " + reason)
            await self.announce(f"⏸ <b>#{c}</b> is waiting.\n💬 Reason: <b>{T.h(reason)}</b>\nSet by {T.mention(members, member['id'])}")
        else:
            await self.change_status(t, "rejected", member, reason=reason, extra="reason: " + reason)
            await self.announce(f"⛔ <b>#{c}</b> was rejected by {T.mention(members, member['id'])}.\n{T.h(t['title'])}\n"
                                f"💬 Reason: <b>{T.h(reason)}</b>\n{T.admins_mention(members)} FYI")

    # ---------- edit ----------
    async def cb_edit(self, q, context, member, parts):
        if parts[1] == "close":
            context.user_data.pop("dl", None)
            return "Closed"
        t = self.task_of(parts)
        if not t:
            return await self.alert(q, "This task does not exist any more.")
        if not self.can_work(member, q.from_user, t):
            return await self.alert(q, "⛔ You cannot edit this task.")
        key = parts[3]
        st = T.STEP_BY_KEY.get(key)
        if not st:
            return await self.alert(q, "Unknown field.")
        chat_id, c = q.message.chat_id, T.code(t["id"])
        if parts[1] == "field":
            if key == "deadline":
                await self.deadline_start(chat_id, context, {"mode": "edit", "task_id": t["id"]})
            elif st.get("text"):
                extra = " Use CAPITAL LETTERS." if key == "company" else ""
                await self.send(chat_id, f"Send the new <b>{st['label']}</b> for #{c}.{extra}\nNow: {T.h(t[key] or '—')}")
                context.user_data["pending"] = {"kind": "edit", "task_id": t["id"], "key": key, "chat_id": chat_id, "msg_id": None}
            else:
                now = T.field_value(self.members(), key, t[key])
                extra = "\n\n" + T.CATEGORY_HELP if key == "category" else ""
                rows = self.option_rows(key, f"e:val:{t['id']}:{key}:") + [[("✖️ Cancel", "e:close")]]
                await self.send(chat_id, f"{st['icon']} New <b>{st['label']}</b> for #{c}?\nNow: {T.h(now)}{extra}", rows)
            return f"{st['icon']} {st['label']}"
        if parts[1] == "val":
            value = self.option_value(key, parts[4])
            if value is None:
                return await self.alert(q, "This choice is not available any more.")
            await self.apply_edit(t, key, value, member)
            return T.field_value(self.members(), key, value)

    async def apply_edit(self, t, key, value, member):
        t = self.db.get_task(t["id"])
        if t[key] == value:
            return
        members = self.members()
        label = T.STEP_BY_KEY[key]["label"]
        old, new = T.field_value(members, key, t[key]), T.field_value(members, key, value)
        self.db.update_field(t["id"], key, value, member["id"], "bot", history_text=f"Changed {label}: {old} → {new}")
        await self.refresh_cards(t["id"])
        extra = f"\n{T.mention(members, value)}, this task is yours now." if key == "assignee" else ""
        await self.announce(f"✏️ {T.mention(members, member['id'])} changed <b>{label}</b> of <b>#{T.code(t['id'])}</b>\n"
                            f"<s>{T.h(old)}</s>\n→ <b>{T.h(new)}</b>{extra}")

    async def add_note(self, t, text, member):
        self.db.add_note(t["id"], text, member["id"], "bot")
        await self.refresh_cards(t["id"])
        await self.announce(f"🗨 <b>Note on #{T.code(t['id'])}</b> from {T.mention(self.members(), member['id'])}:\n{T.h(text)}")

    # ================= check-ins =================
    def checkin_rows(self, task_id, round_id):
        b = lambda ans: (ANSWER[ans], f"ci:{ans}:{task_id}:{round_id}")
        return [[b("progress"), b("block")], [b("done"), b("reject")], [("📝 Add note", f"ci:note:{task_id}:{round_id}")]]

    def who_line(self, t, members):
        return "👥 Everyone" if t["assignee"] == T.ALL else "👤 " + T.mention(members, t["assignee"])

    async def cb_checkin(self, q, context, member, parts):
        ans, t = parts[1], self.task_of(parts)
        if not t:
            return await self.alert(q, "This task does not exist any more.")
        round_id, user, chat_id = int(parts[3]), q.from_user, q.message.chat_id
        members = self.members()
        if not self.can_work(member, user, t):
            return await self.alert(q, f"⛔ This is {T.name_of(members, t['assignee'])}'s task. Only they, the creator or an admin can answer.")
        c = T.code(t["id"])
        if ans == "note":
            await self.send(chat_id, f"📝 {T.mention(members, member['id'])}, write an update for <b>#{c}</b>: "
                                     "what was done today, or why it is not finished.")
            context.user_data["pending"] = {"kind": "note", "task_id": t["id"], "chat_id": chat_id, "msg_id": None}
            return
        if self.db.get_answer(round_id, t["id"]):
            return await self.alert(q, "Already answered.")
        if t["status"] not in OPEN:
            return await self.alert(q, "This task is already " + T.STATUS[t["status"]])
        if ans == "block" and t["status"] == "blocked":
            self.db.add_history(t["id"], member["id"], f"Check-in: still waiting ({t['reason']})")
        elif ans == "progress":
            if t["status"] != "progress":
                await self.change_status(t, "progress", member, extra="check-in")
            else:
                self.db.add_history(t["id"], member["id"], "Check-in: still in progress")
            if T.is_overdue(t, self.today()):
                await self.send(chat_id, f"🔴 <b>#{c}</b> is overdue. The deadline was {T.fmt_date(t['deadline'])}.\n"
                                         f"{T.mention(members, member['id'])}, write why it is not completed yet. "
                                         "<b>This note is required.</b>")
                context.user_data["pending"] = {"kind": "note", "task_id": t["id"], "chat_id": chat_id, "msg_id": None}
        elif ans == "done":
            if await self.do_done(q, t, member) is False:
                return False
        elif ans in ("block", "reject"):
            if await self.ask_reason(q, ans, t, chat_id) is False:
                return False
        self.db.record_answer(round_id, t["id"], member["id"], ans)
        if isinstance(q.message, Message):
            line = f"\n\n✔️ <b>{ANSWER[ans]}</b> · answered by {T.h(member['name'])} at {self.db.now():%H:%M}"
            await self.edit(chat_id, q.message.message_id, (q.message.text_html or "") + line)

    async def job_checkin(self, context):
        await self.run_checkin(context.job.data)

    async def run_checkin(self, label):
        gid = self.group_id()
        if not gid:
            log.warning("Check-in %s skipped: no team group set", label)
            return
        tasks = [t for t in self.db.tasks() if t["status"] in OPEN]
        round_id = self.db.new_round(label)
        if not tasks:
            await self.announce("🎉 No open tasks right now. Good job, team.")
            return
        members, today = self.members(), self.today()
        icon = "🕐" if int(label[:2]) < 12 else "🕔"
        await self.announce(f"{icon} <b>{label} check-in</b> · {len(tasks)} open task{'s' if len(tasks) > 1 else ''}\n"
                            f"Press a button under your task.\nNo answer in {self.cfg.remind_after_min} min: I remind you.\n"
                            f"No answer in {self.cfg.escalate_after_min} min: I tell {T.admins_mention(members)}.")
        for t in tasks:
            reason = f" ({T.h(t['reason'])})" if t["status"] == "blocked" and t["reason"] else ""
            over = " 🔴 OVERDUE" if T.is_overdue(t, today) else ""
            await self.announce(f"❓ <b>#{T.code(t['id'])}</b> · {T.h(t['title'])}\n{self.who_line(t, members)} · "
                                f"⏰ {T.fmt_date(t['deadline'], year=False)}{over}\nNow: {T.STATUS[t['status']]}{reason}\n\n"
                                "<b>What is the status?</b>", self.checkin_rows(t["id"], round_id))
        data = {"round": round_id, "label": label, "tasks": [t["id"] for t in tasks]}
        jq = self.app.job_queue
        jq.run_once(self.job_remind, when=timedelta(minutes=self.cfg.remind_after_min), data=data)
        jq.run_once(self.job_escalate, when=timedelta(minutes=self.cfg.escalate_after_min), data=data)

    def unanswered(self, data):
        out = []
        for tid in data["tasks"]:
            t = self.db.get_task(tid)
            if t and t["status"] in OPEN and not self.db.get_answer(data["round"], tid):
                out.append(t)
        return out

    async def job_remind(self, context):
        data = context.job.data
        members = self.members()
        for t in self.unanswered(data):
            who = "everyone" if t["assignee"] == T.ALL else T.mention(members, t["assignee"])
            await self.announce(f"⏰ <b>Reminder</b> {who}\n<b>#{T.code(t['id'])}</b> · {T.h(t['title'])}\n"
                                f"You did not answer the {data['label']} check-in.\n\n<b>What is the status?</b>",
                                self.checkin_rows(t["id"], data["round"]))

    async def job_escalate(self, context):
        data = context.job.data
        missing = self.unanswered(data)
        if not missing:
            return
        members = self.members()
        lines = "\n".join(f"• <b>#{T.code(t['id'])}</b> · {T.h(t['title'])} · {T.mention(members, t['assignee'])}" for t in missing)
        await self.announce(f"🚨 <b>No update for {self.cfg.escalate_after_min} min</b>\n{T.admins_mention(members)}\n"
                            f"These tasks got no answer to the {data['label']} check-in:\n\n{lines}",
                            [[(f"👀 Open #{T.code(t['id'])}", f"c:show:{t['id']}")] for t in missing[:20]])

    async def job_overdue(self, context):
        if not self.group_id():
            return
        tasks = [t for t in self.db.tasks() if T.is_overdue(t, self.today())]
        if not tasks:
            return
        round_id = self.db.new_round("overdue")
        members = self.members()
        for t in tasks:
            who = "everyone" if t["assignee"] == T.ALL else T.mention(members, t["assignee"])
            await self.announce(
                f"🔴 <b>OVERDUE</b> · <b>#{T.code(t['id'])}</b>\n{T.h(t['title'])}\nThe deadline was <b>{T.fmt_date(t['deadline'])}</b>.\n"
                f"{who}, what is the status? If it is still in progress, write why.\n{T.admins_mention(members)} FYI\n\n"
                f"<i>I repeat this every {self.cfg.overdue_every_hours} hours until the task is closed or gets a new deadline.</i>",
                self.checkin_rows(t["id"], round_id))


def strip_tags(s):
    """Alerts are plain text: drop HTML tags."""
    return re.sub(r"<[^>]+>", "", s)
