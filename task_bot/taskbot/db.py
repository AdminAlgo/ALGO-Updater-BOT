"""SQLite storage: members, tasks, history, check-in answers.

Every change to a task writes a history row with a `source`:
'bot' rows are shown in Telegram, 'web' rows only on the admin website.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

OPEN = ("new", "progress", "blocked")
CLOSED = ("done", "rejected")
STATUSES = ("new", "progress", "blocked", "approval", "done", "rejected")
TASK_FIELDS = ("title", "description", "assignee", "deadline", "priority", "company", "category", "notes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  tg_username TEXT,
  tg_user_id INTEGER UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL,
  deadline TEXT NOT NULL,
  priority TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',
  reason TEXT NOT NULL DEFAULT '',
  last_note TEXT NOT NULL DEFAULT '',
  last_note_by TEXT NOT NULL DEFAULT '',
  last_note_at TEXT NOT NULL DEFAULT '',
  creator TEXT NOT NULL,
  done_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  at TEXT NOT NULL,
  who TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'bot'
);
CREATE INDEX IF NOT EXISTS history_task ON history(task_id);
CREATE TABLE IF NOT EXISTS card_messages (
  task_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
  round_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  member_id TEXT NOT NULL,
  answer TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (round_id, task_id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path, tz, clock=None):
        self.path = path
        self.tz = tz
        self._clock = clock
        self._lock = threading.RLock()
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    # ---------- plumbing ----------
    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def now(self):
        return self._clock() if self._clock else datetime.now(self.tz)

    def stamp(self):
        return self.now().strftime("%Y-%m-%d %H:%M")

    def today(self):
        return self.now().strftime("%Y-%m-%d")

    # ---------- settings ----------
    def get_setting(self, key, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))

    # ---------- members ----------
    def seed_members(self, path):
        with self.conn() as c:
            if c.execute("SELECT COUNT(*) FROM members").fetchone()[0]:
                return 0
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        for r in rows:
            self.add_member(r["id"], r["name"], bool(r.get("is_admin")), r.get("tg_username") or "")
        return len(rows)

    def add_member(self, member_id, name, is_admin=False, tg_username=""):
        member_id = member_id.strip().upper()
        with self.conn() as c:
            c.execute("INSERT INTO members(id,name,is_admin,tg_username,created_at) VALUES(?,?,?,?,?)",
                      (member_id, name.strip(), 1 if is_admin else 0, tg_username.strip().lstrip("@"), self.stamp()))
        return member_id

    def update_member(self, member_id, name, is_admin, tg_username, active):
        with self.conn() as c:
            c.execute("UPDATE members SET name=?, is_admin=?, tg_username=?, active=? WHERE id=?",
                      (name.strip(), 1 if is_admin else 0, tg_username.strip().lstrip("@"), 1 if active else 0, member_id))

    def unlink_member(self, member_id):
        with self.conn() as c:
            c.execute("UPDATE members SET tg_user_id=NULL WHERE id=?", (member_id,))

    def get_member(self, member_id):
        if not member_id:
            return None
        with self.conn() as c:
            return c.execute("SELECT * FROM members WHERE id=?", (member_id.strip().upper(),)).fetchone()

    def member_by_tg(self, tg_user_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM members WHERE tg_user_id=? AND active=1", (tg_user_id,)).fetchone()

    def member_by_username(self, username):
        username = (username or "").strip().lstrip("@")
        if not username:
            return None
        with self.conn() as c:
            return c.execute("SELECT * FROM members WHERE lower(tg_username)=lower(?) AND active=1", (username,)).fetchone()

    def auto_member(self, tg_user_id, username, name):
        """The member for a Telegram account, found by account or username, or created. No employee ID needed.
        Returns None when this account belongs to a switched-off member."""
        m = self.member_by_tg(tg_user_id)
        if m:
            return m
        with self.conn() as c:
            if c.execute("SELECT 1 FROM members WHERE tg_user_id=?", (tg_user_id,)).fetchone():
                return None  # linked but switched off
        m = self.member_by_username(username)
        if m and not m["tg_user_id"]:
            self.link_member(m["id"], tg_user_id, username)
            return self.member_by_tg(tg_user_id)
        member_id = f"TG{tg_user_id}"
        if not self.get_member(member_id):
            self.add_member(member_id, (name or "Member").strip()[:40] or "Member", False, username or "")
        with self.conn() as c:
            c.execute("UPDATE members SET tg_user_id=? WHERE id=?", (tg_user_id, member_id))
        return self.member_by_tg(tg_user_id)

    def members(self, active_only=True):
        q = "SELECT * FROM members" + (" WHERE active=1" if active_only else "") + " ORDER BY is_admin DESC, created_at, id"
        with self.conn() as c:
            return c.execute(q).fetchall()

    def link_member(self, member_id, tg_user_id, tg_username):
        """Link a Telegram account to an employee ID. Returns 'ok', 'unknown', 'taken' or 'inactive'."""
        m = self.get_member(member_id)
        if not m:
            return "unknown"
        if not m["active"]:
            return "inactive"
        if m["tg_user_id"] and m["tg_user_id"] != tg_user_id:
            return "taken"
        with self.conn() as c:
            c.execute("UPDATE members SET tg_user_id=NULL WHERE tg_user_id=? AND id<>?", (tg_user_id, m["id"]))
            c.execute("UPDATE members SET tg_user_id=?, tg_username=COALESCE(NULLIF(?,''), tg_username) WHERE id=?",
                      (tg_user_id, (tg_username or "").lstrip("@"), m["id"]))
        return "ok"

    # ---------- tasks ----------
    def create_task(self, data, creator, source="bot"):
        now = self.stamp()
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO tasks(title,description,assignee,deadline,priority,company,category,notes,status,creator,"
                "created_at,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,'new',?,?,?,?)",
                (data["title"].strip(), data.get("description", "").strip(), data["assignee"], data["deadline"],
                 data["priority"], data.get("company", "").strip(), data["category"], data.get("notes", "").strip(),
                 creator, now, now, creator))
            task_id = cur.lastrowid
            c.execute("INSERT INTO history(task_id,at,who,text,source) VALUES(?,?,?,?,?)",
                      (task_id, now, creator, "Created task" + (" on the website" if source == "web" else ""), source))
        return task_id

    def get_task(self, task_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM tasks ORDER BY id").fetchall()

    def _touch(self, c, task_id, who, now, source="bot"):
        # Website changes stay invisible in Telegram, so they do not move "Last update".
        if source == "bot":
            c.execute("UPDATE tasks SET updated_at=?, updated_by=? WHERE id=?", (now, who, task_id))

    def add_history(self, task_id, who, text, source="bot", touch=True):
        now = self.stamp()
        with self.conn() as c:
            c.execute("INSERT INTO history(task_id,at,who,text,source) VALUES(?,?,?,?,?)", (task_id, now, who, text, source))
            if touch:
                self._touch(c, task_id, who, now, source)

    def update_field(self, task_id, field, value, who, source="bot", history_text=None):
        if field not in TASK_FIELDS:
            raise ValueError(field)
        now = self.stamp()
        with self.conn() as c:
            c.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, task_id))
            if history_text:
                c.execute("INSERT INTO history(task_id,at,who,text,source) VALUES(?,?,?,?,?)",
                          (task_id, now, who, history_text, source))
            self._touch(c, task_id, who, now, source)

    def set_status(self, task_id, status, who, source="bot", reason=None, history_text=None):
        if status not in STATUSES:
            raise ValueError(status)
        now = self.stamp()
        with self.conn() as c:
            sets, args = ["status=?"], [status]
            if reason is not None:
                sets.append("reason=?")
                args.append(reason)
            elif status in ("progress", "approval", "done"):
                sets.append("reason=''")
            if status == "approval":
                sets.append("done_by=?")
                args.append(who)
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, task_id))
            if history_text:
                c.execute("INSERT INTO history(task_id,at,who,text,source) VALUES(?,?,?,?,?)",
                          (task_id, now, who, history_text, source))
            self._touch(c, task_id, who, now, source)

    def set_reason(self, task_id, reason):
        with self.conn() as c:
            c.execute("UPDATE tasks SET reason=? WHERE id=?", (reason, task_id))

    def add_note(self, task_id, text, who, source="bot"):
        now = self.stamp()
        with self.conn() as c:
            c.execute("UPDATE tasks SET last_note=?, last_note_by=?, last_note_at=? WHERE id=?", (text, who, now, task_id))
            c.execute("INSERT INTO history(task_id,at,who,text,source) VALUES(?,?,?,?,?)",
                      (task_id, now, who, "Note: " + text, source))
            self._touch(c, task_id, who, now, source)

    def history(self, task_id, include_web=False):
        q = "SELECT * FROM history WHERE task_id=?" + ("" if include_web else " AND source='bot'") + " ORDER BY id"
        with self.conn() as c:
            return c.execute(q, (task_id,)).fetchall()

    def all_history(self, limit=500):
        with self.conn() as c:
            return c.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------- telegram card messages ----------
    def add_card(self, task_id, chat_id, message_id):
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO card_messages(task_id,chat_id,message_id) VALUES(?,?,?)",
                      (task_id, chat_id, message_id))

    def cards(self, task_id):
        with self.conn() as c:
            return c.execute("SELECT chat_id, message_id FROM card_messages WHERE task_id=?", (task_id,)).fetchall()

    def drop_card(self, chat_id, message_id):
        with self.conn() as c:
            c.execute("DELETE FROM card_messages WHERE chat_id=? AND message_id=?", (chat_id, message_id))

    # ---------- check-in rounds ----------
    def new_round(self, label):
        with self.conn() as c:
            return c.execute("INSERT INTO rounds(label,started_at) VALUES(?,?)", (label, self.stamp())).lastrowid

    def get_round(self, round_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()

    def record_answer(self, round_id, task_id, member_id, answer):
        """False if this task already has an answer in this round."""
        try:
            with self.conn() as c:
                c.execute("INSERT INTO answers(round_id,task_id,member_id,answer,at) VALUES(?,?,?,?,?)",
                          (round_id, task_id, member_id, answer, self.stamp()))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_answer(self, round_id, task_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM answers WHERE round_id=? AND task_id=?", (round_id, task_id)).fetchone()
