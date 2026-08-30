"""Per-driver group registry + Telegram group auto-discovery.

Maps each driver to their own Telegram group so a driver's early-warning alerts
(2h / 1h) go to THEIR group. Built by matching a group's title to a driver by
full name (preferred) or truck number, as the bot is added to / messaged in
each group.

Persisted to a JSON file:
  {
    "offset": <last processed Telegram update_id>,
    "drivers": { "<driver_id>": {"chat_id": "...", "title": "...",
                                 "matched_on": "name|truck|username",
                                 "driver_name": "..."} }
  }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .eld.base import normalize_name

log = logging.getLogger("eld_alert_bot")


@dataclass(frozen=True)
class Candidate:
    """A driver the discovery step can match a group title against."""
    driver_id: str
    name: str
    username: str | None = None
    truck: str | None = None


def match_title(title: str, candidates: list[Candidate]) -> tuple[str | None, str | None]:
    """Match a group title to a driver. Returns (driver_id, matched_on) or (None, None).

    Priority: full name (substring) → truck number (whole token) → username.
    """
    t = normalize_name(title)
    if not t:
        return None, None

    # 1) full name appears in the title
    for c in candidates:
        if c.name and normalize_name(c.name) in t:
            return c.driver_id, "name"

    # 2) truck number as a standalone token (avoid '18' matching '1833')
    for c in candidates:
        if c.truck and re.search(rf"(?<!\d){re.escape(str(c.truck))}(?!\d)", t):
            return c.driver_id, "truck"

    # 3) username
    for c in candidates:
        if c.username and normalize_name(c.username) in t:
            return c.driver_id, "username"

    return None, None


def match_drivers(title: str, candidates: list[Candidate]) -> list[tuple[str, str]]:
    """Match a group title to ALL drivers it names (co-driver / team groups).

    Returns a list of (driver_id, matched_on). A team group titled with two
    drivers registers both. Priority tier is name → truck → username: as soon as
    one tier produces matches, those are returned (so a names-titled group never
    also pulls in unrelated truck matches).
    """
    t = normalize_name(title)
    if not t:
        return []
    by_name = [(c.driver_id, "name") for c in candidates if c.name and normalize_name(c.name) in t]
    if by_name:
        return by_name
    by_truck = [(c.driver_id, "truck") for c in candidates
                if c.truck and re.search(rf"(?<!\d){re.escape(str(c.truck))}(?!\d)", t)]
    if by_truck:
        return by_truck
    return [(c.driver_id, "username") for c in candidates
            if c.username and normalize_name(c.username) in t]


class GroupRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._offset: int = 0
        self._drivers: dict[str, dict] = {}
        # Drivers explicitly removed via /remove — auto-registration skips them
        # until /add re-enables (otherwise a matching group title would silently
        # re-register them on the next message).
        self._blocked: set[str] = set()
        if self._path and self._path.exists():
            self.load()

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8")) or {}
            self._offset = int(data.get("offset", 0))
            self._drivers = data.get("drivers", {}) or {}
            self._blocked = set(data.get("blocked", []) or [])
        except (json.JSONDecodeError, OSError, ValueError):
            self._offset, self._drivers, self._blocked = 0, {}, set()

    def save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.tmp")
            temporary.write_text(
                json.dumps({"offset": self._offset, "drivers": self._drivers,
                            "blocked": sorted(self._blocked)}, indent=2),
                "utf-8",
            )
            temporary.replace(self._path)

    # --- offset (Telegram update ack) --- #
    @property
    def offset(self) -> int:
        return self._offset

    def set_offset(self, value: int) -> None:
        self._offset = value

    # --- mapping --- #
    def chat_for(self, driver_id: str) -> str | None:
        rec = self._drivers.get(driver_id)
        return rec["chat_id"] if rec else None

    def is_registered(self, driver_id: str) -> bool:
        return driver_id in self._drivers

    def register(self, driver_id: str, chat_id: str, title: str,
                 matched_on: str, driver_name: str) -> bool:
        """Record/refresh a driver's group. Returns True if newly added/changed.

        Preserves any captured Telegram tag (tg_username / tg_user_id).
        """
        prev = self._drivers.get(driver_id) or {}
        changed = not prev or prev.get("chat_id") != str(chat_id)
        self._drivers[driver_id] = {
            "chat_id": str(chat_id),
            "title": title,
            "matched_on": matched_on,
            "driver_name": driver_name,
            "tg_username": prev.get("tg_username"),
            "tg_user_id": prev.get("tg_user_id"),
        }
        return changed

    # --- driver Telegram tag (for @mentions) --- #
    def driver_for_chat(self, chat_id) -> str | None:
        for did, rec in self._drivers.items():
            if str(rec.get("chat_id")) == str(chat_id):
                return did
        return None

    def drivers_for_chat(self, chat_id) -> list[str]:
        """All driver ids registered to a chat (co-driver / team groups)."""
        return [did for did, rec in self._drivers.items()
                if str(rec.get("chat_id")) == str(chat_id)]

    def unregister(self, driver_id: str) -> bool:
        """Remove a driver's registration entirely. Returns True if it existed."""
        return self._drivers.pop(driver_id, None) is not None

    # --- explicit removal blocklist (survives auto re-registration) --- #
    def block(self, driver_id: str) -> None:
        self._blocked.add(driver_id)

    def unblock(self, driver_id: str) -> None:
        self._blocked.discard(driver_id)

    def is_blocked(self, driver_id: str) -> bool:
        return driver_id in self._blocked

    def driver_name(self, driver_id: str) -> str | None:
        rec = self._drivers.get(driver_id)
        return rec.get("driver_name") if rec else None

    def set_tag(self, driver_id: str, username: str | None, user_id) -> bool:
        """Store a driver's Telegram @username / id. Returns True if changed."""
        rec = self._drivers.get(driver_id)
        if not rec:
            return False
        changed = rec.get("tg_username") != username or rec.get("tg_user_id") != user_id
        rec["tg_username"] = username
        rec["tg_user_id"] = user_id
        return changed

    def tag_username(self, driver_id: str) -> str | None:
        rec = self._drivers.get(driver_id)
        return (rec.get("tg_username") if rec else None) or None

    def tag_user_id(self, driver_id: str):
        rec = self._drivers.get(driver_id)
        return rec.get("tg_user_id") if rec else None

    def count(self) -> int:
        return len(self._drivers)

    def coverage(self) -> dict:
        """Tag coverage summary: how many drivers have a tag, and which don't."""
        by_u = by_i = 0
        missing: list[str] = []
        for rec in self._drivers.values():
            if rec.get("tg_username"):
                by_u += 1
            elif rec.get("tg_user_id"):
                by_i += 1
            else:
                missing.append(rec.get("driver_name") or "?")
        return {
            "total": len(self._drivers),
            "tagged": by_u + by_i,
            "by_username": by_u,
            "by_id": by_i,
            "missing": sorted(missing),
        }

    def all(self) -> dict[str, dict]:
        return dict(self._drivers)


def _name_matches(driver_name: str, sender_name: str) -> bool:
    """True if every token of the driver's name appears in the sender's name."""
    sn = normalize_name(sender_name)
    toks = [t for t in normalize_name(driver_name).split() if t]
    return bool(toks) and all(t in sn for t in toks)


def _chat_from_update(u: dict) -> dict | None:
    for key in ("message", "my_chat_member", "edited_message", "channel_post"):
        chat = (u.get(key) or {}).get("chat")
        if chat:
            return chat
    return None


def _coverage_report(registry: GroupRegistry) -> str:
    c = registry.coverage()
    lines = [
        "📊 Tag coverage",
        f"Registered driver groups: {c['total']}",
        f"Tagged: {c['tagged']}  (@username {c['by_username']}, by id {c['by_id']})",
        f"Missing tag: {len(c['missing'])}",
    ]
    if c["missing"]:
        shown = c["missing"][:30]
        lines.append("Missing: " + ", ".join(shown) + (" …" if len(c["missing"]) > 30 else ""))
    return "\n".join(lines)


def _admin_help() -> str:
    return (
        "Admin controls:\n"
        "/coverage — show driver-group coverage\n"
        "/drivers — list registered driver groups\n"
        "/add <driver name> — assign the driver to this group\n"
        "/remove <driver name> — remove the driver from this group"
    )


def _driver_report(registry: GroupRegistry) -> str:
    records = registry.all()
    if not records:
        return "No driver groups are registered yet."
    lines = ["Registered driver groups:"]
    for record in sorted(records.values(), key=lambda item: item.get("driver_name", "")):
        lines.append(
            f"- {record.get('driver_name', '?')} -> {record.get('title') or record.get('chat_id')}"
        )
    return "\n".join(lines)


def discover_groups(sender, registry: GroupRegistry, candidates: list[Candidate],
                    reply: bool = True, admin_user_ids=None) -> list[tuple]:
    """Process pending Telegram updates and register matched driver groups.

    Returns a list of (driver_name, chat_id, matched_on, title) newly registered
    or re-pointed. Advances and persists the update offset.
    """
    updates = sender.get_updates(registry.offset)
    if not updates:
        return []

    newly: list[tuple] = []
    max_id = registry.offset
    for u in updates:
        max_id = max(max_id, int(u.get("update_id", 0)))
        chat = _chat_from_update(u)
        if not chat:
            continue

        # (0) Handle commands (in any chat).
        msg = u.get("message") or u.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        sender_id = (msg.get("from") or {}).get("id")
        admin_only = bool(admin_user_ids)
        is_admin = not admin_only or sender_id in admin_user_ids
        if cmd == "/admin":
            if is_admin:
                sender.send_message(str(chat["id"]), _admin_help())
            continue
        if cmd == "/drivers":
            if is_admin:
                sender.send_message(str(chat["id"]), _driver_report(registry))
            continue
        if cmd in ("/status", "/coverage"):
            sender.send_message(str(chat["id"]), _coverage_report(registry))
            continue
        if cmd in ("/remove", "/unregister"):
            if not is_admin:
                sender.send_message(str(chat["id"]), "This command is restricted to bot administrators.")
                continue
            arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if not arg:
                sender.send_message(str(chat["id"]),
                                    "Usage: /remove <driver name> — stops that driver's "
                                    "alerts in this group.")
            else:
                removed = []
                for did in list(registry.drivers_for_chat(chat["id"])):
                    dn = registry.driver_name(did) or ""
                    if _name_matches(dn, arg) or _name_matches(arg, dn):
                        registry.unregister(did)
                        registry.block(did)  # keep it removed even if the title still matches
                        removed.append(dn)
                if removed:
                    log.info("removed via command: %s from chat %s", removed, chat["id"])
                    sender.send_message(
                        str(chat["id"]),
                        f"✅ Removed {', '.join(removed)} from this group. "
                        f"No more alerts for them here. (Use /add <name> to bring them back.)")
                else:
                    sender.send_message(str(chat["id"]),
                                        f"No registered driver matching “{arg}” in this group.")
            continue
        if cmd in ("/add", "/register"):
            if not is_admin:
                sender.send_message(str(chat["id"]), "This command is restricted to bot administrators.")
                continue
            arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if not arg:
                sender.send_message(str(chat["id"]),
                                    "Usage: /add <driver name> — registers that driver's "
                                    "alerts to this group.")
            else:
                added = []
                for c in candidates:
                    if _name_matches(c.name, arg) or _name_matches(arg, c.name):
                        registry.unblock(c.driver_id)
                        registry.register(c.driver_id, chat["id"],
                                          chat.get("title") or "", "manual", c.name)
                        added.append(c.name)
                if added:
                    log.info("added via command: %s to chat %s", added, chat["id"])
                    sender.send_message(
                        str(chat["id"]),
                        f"✅ Registered {', '.join(added)} to this group. "
                        f"HOS alerts will be posted here.")
                else:
                    sender.send_message(str(chat["id"]),
                                        f"No driver matching “{arg}” found on the roster.")
            continue

        if chat.get("type") not in ("group", "supergroup"):
            continue

        # (a) Register the group to ALL drivers it names (co-driver teams too).
        title = chat.get("title") or ""
        new_names: list[str] = []
        for driver_id, how in match_drivers(title, candidates):
            if registry.is_blocked(driver_id):  # explicitly /remove'd — stay out
                continue
            name = next((c.name for c in candidates if c.driver_id == driver_id), "")
            if registry.register(driver_id, chat["id"], title, how, name):
                newly.append((name, chat["id"], how, title))
                new_names.append(name)
        if new_names and reply:
            who = "these drivers" if len(new_names) > 1 else "this driver"
            sender.send_message(
                str(chat["id"]),
                f"✅ Registered this group to {', '.join(new_names)}. "
                f"HOS alerts for {who} will be posted here.",
            )

        # (b) Auto-capture a driver's Telegram @tag: match a message sender's
        # name to whichever co-driver registered to this group.
        msg = u.get("message") or u.get("edited_message") or {}
        frm = msg.get("from") or {}
        if frm and not frm.get("is_bot"):
            sender_name = f"{frm.get('first_name', '')} {frm.get('last_name', '')}".strip()
            for did in registry.drivers_for_chat(chat["id"]):
                if _name_matches(registry.driver_name(did) or "", sender_name):
                    if registry.set_tag(did, frm.get("username"), frm.get("id")):
                        handle = ("@" + frm["username"]) if frm.get("username") else f"id:{frm.get('id')}"
                        log.info("captured tag for %s -> %s",
                                 registry.driver_name(did), handle)
                    break

    registry.set_offset(max_id + 1)
    registry.save()
    return newly
