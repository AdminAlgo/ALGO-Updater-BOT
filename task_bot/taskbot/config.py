"""Settings, read from environment variables (Railway -> Variables)."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _int(name, default):
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def parse_times(raw):
    """'17:00,01:00' -> ((17, 0), (1, 0))."""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hh, mm = part.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"bad time {part!r}")
        out.append((h, m))
    return tuple(out)


@dataclass(frozen=True)
class Config:
    token: str
    admin_usernames: frozenset
    creator_usernames: frozenset
    tz: ZoneInfo
    checkin_times: tuple
    remind_after_min: int
    escalate_after_min: int
    overdue_every_hours: int
    data_dir: str
    port: int
    dashboard_username: str
    dashboard_password: str
    secret_key: str
    group_chat_id: int | None

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "tasks.db")


def load():
    group = os.environ.get("GROUP_CHAT_ID", "").strip()
    admins = os.environ.get("ADMIN_USERNAMES", "milord0_0,Abdulakhatov04")
    return Config(
        token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        admin_usernames=frozenset(u.strip().lstrip("@").lower() for u in admins.split(",") if u.strip()),
        creator_usernames=frozenset(u.strip().lstrip("@").lower()
                                    for u in os.environ.get("TASK_CREATOR_USERNAMES", "").split(",") if u.strip()),
        tz=ZoneInfo(os.environ.get("TIMEZONE", "Asia/Tashkent").strip() or "Asia/Tashkent"),
        checkin_times=parse_times(os.environ.get("CHECKIN_TIMES", "17:00,01:00")),
        remind_after_min=_int("REMIND_AFTER_MIN", 30),
        escalate_after_min=_int("ESCALATE_AFTER_MIN", 60),
        overdue_every_hours=_int("OVERDUE_EVERY_HOURS", 3),
        data_dir=os.environ.get("DATA_DIR", "./data").strip() or "./data",
        port=_int("PORT", 8080),
        dashboard_username=os.environ.get("DASHBOARD_USERNAME", "").strip() or "admin",
        dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
        secret_key=os.environ.get("SECRET_KEY", ""),
        group_chat_id=int(group) if group else None,
    )


def problems(cfg):
    """Human-readable list of missing settings."""
    out = []
    if not cfg.token:
        out.append("TELEGRAM_BOT_TOKEN is empty (get it from @BotFather).")
    if not cfg.dashboard_password:
        out.append("DASHBOARD_PASSWORD is empty (password for the admin website).")
    if len(cfg.secret_key) < 16:
        out.append("SECRET_KEY must be at least 16 characters of random text.")
    return out
