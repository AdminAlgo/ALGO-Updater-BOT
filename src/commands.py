"""Telegram inbound handling — commands, group discovery, @tag capture.

Single place every Telegram update is processed. Two entry points:

  process_updates(...)   one sweep over pending getUpdates — used by the
                         `--discover` CLI and registry.discover_groups().

  run_command_loop(...)  a continuous long-poll loop for the live service, so
                         PM commands and group assignment respond in ~1-2s
                         instead of waiting a full DriveHOS poll cycle. Runs on
                         its own thread and is the ONLY getUpdates consumer while
                         active (a second poller gets HTTP 409 from Telegram), so
                         the scheduler is started with discover=False.

Assignment model: one Telegram group per driver. A group auto-registers only
when its title identifies exactly one roster driver (registry.match_unique);
anything ambiguous is parked in the registry's pending queue for a human to
resolve with /assign or the dashboard.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from datetime import datetime, timezone

from .eld.base import normalize_name
from .registry import Candidate, GroupRegistry, _name_matches, match_unique

log = logging.getLogger("eld_alert_bot")

_CHAT_ID_CHARS = set("-0123456789")

# Slash-command menu shown in Telegram's compose box (setMyCommands).
_BOT_COMMANDS = [
    ("faq", "How the alerts work — what, when, to whom"),
    ("roster", "Find a driver: /roster <name or truck>"),
    ("groups", "Every group I'm in + who it's assigned to"),
    ("assign", "Assign a driver: /assign <name> | <group-id>"),
    ("unassign", "Remove a driver's group: /unassign <name>"),
    ("whois", "Who a group is assigned to: /whois <group-id>"),
    ("unassigned", "Groups still waiting to be assigned"),
    ("coverage", "@-tag coverage summary"),
    ("add", "In a driver's group: /add <name>"),
    ("remove", "In a driver's group: /remove <name>"),
    ("help", "Show the admin controls"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_like_chat_id(token: str) -> bool:
    t = (token or "").strip()
    return len(t) >= 5 and set(t) <= _CHAT_ID_CHARS and t.lstrip("-").isdigit()


def _short_id(driver_id: str) -> str:
    return str(driver_id)[-6:]


def _chat_from_update(u: dict) -> dict | None:
    for key in ("message", "my_chat_member", "edited_message", "channel_post"):
        chat = (u.get(key) or {}).get("chat")
        if chat:
            return chat
    return None


def _bot_membership_change(u: dict) -> str | None:
    """'joined' / 'left' / None from a my_chat_member update about the bot."""
    mcm = u.get("my_chat_member")
    if not mcm:
        return None
    new = (mcm.get("new_chat_member") or {}).get("status")
    if new in ("member", "administrator", "creator"):
        return "joined"
    if new in ("left", "kicked"):
        return "left"
    return None


def _roster_search(query: str, candidates: list[Candidate]) -> list[Candidate]:
    """Loose driver lookup for /roster and /assign: token-subset OR substring on
    the name, exact truck number, or a driver-id suffix."""
    q = normalize_name(query)
    if not q:
        return []
    hits: list[Candidate] = []
    for c in candidates:
        n = normalize_name(c.name)
        if (
            q in n
            or _name_matches(c.name, query)
            or _name_matches(query, c.name)
            or (c.truck and q == str(c.truck).lower())
            or str(c.driver_id).lower().endswith(q)
        ):
            hits.append(c)
    return hits


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def _help_text() -> str:
    return (
        "🛠 ALGO Updater — admin controls\n\n"
        "In a driver's group:\n"
        "  /add <name> — send this driver's HOS alerts here\n"
        "  /remove <name> — stop this driver's alerts here\n\n"
        "Anywhere (PM me):\n"
        "  /roster <search> — find a driver (name / truck)\n"
        "  /groups — every group I'm in + who it's assigned to\n"
        "  /assign <name> | <group-id> — assign a driver to a group\n"
        "  /unassign <name> — remove a driver's group\n"
        "  /whois <group-id> — who a group is assigned to\n"
        "  /unassigned — groups still waiting to be assigned\n"
        "  /coverage — @-tag coverage summary\n"
        "  /faq — how the alerts work (what, when, to whom)"
    )


def _faq_text() -> str:
    """Plain-text reference of the alert logic — served by /faq and kept in
    sync with rules.py + config.yaml. Stays under Telegram's 4096-char limit."""
    return (
        "📖 ALGO Updater — how the alerts work\n"
        "———————————————————————\n"
        "I pull live Hours-of-Service data for every active driver every "
        "2 minutes and re-check every rule. Commands you send me are answered "
        "in 1–2 seconds.\n\n"

        "1) LOW HOURS — driver is running out of time\n"
        "• Fires when the tightest of Drive / Break / Shift time-remaining drops "
        "to 2 h, then 1 h, then 30 min left.\n"
        "• → the driver's own group. The dispatch/team group also gets the 1 h "
        "and 30 min warnings.\n"
        "• Each level sends once per shift; resets after a reset/recap or when "
        "the driver goes Off Duty.\n"
        "• Only while Driving / On Duty / Yard Move.\n\n"

        "2) 70-HOUR CYCLE — running low\n"
        "• Fires when Cycle time-remaining drops to 10 h, then 5 h left.\n"
        "• → the driver's own group. Each level sends once per cycle; resets "
        "on a 34-hour restart or when the driver goes Off Duty.\n\n"

        "3) 14-HOUR SHIFT LIMIT — violation\n"
        "• Fires the moment Shift time-remaining hits 0 while on duty.\n"
        "• → the driver's own group. Repeats every 30 min while still in "
        "violation, up to 5 times, then stops.\n\n"

        "4) ELD DISCONNECTED — device offline while working\n"
        "• Fires when the truck is OFFLINE or has not reported for 30 min, "
        "while the driver is Driving / On Duty / Yard Move.\n"
        "• → the driver's group. Repeats every 60 min until reconnected.\n"
        "• Never off duty, in sleeper, or with no truck paired.\n\n"

        "5) LONG ON-DUTY — welfare check\n"
        "• Fires when a driver is On Duty (not driving) for 2 h straight.\n"
        "• → the driver's group. Once per On-Duty stretch.\n\n"

        "WHAT DOES NOT ALERT\n"
        "• Off-duty / sleeper drivers (resting).\n"
        "• A driver who just came On Duty with a fresh shift (no false 14 h).\n"
        "• A driver with no group assigned — none of their alerts have "
        "anywhere to go, including the 14 h violation.\n"
        "• A disabled company.\n\n"

        "TIMING & REPEATS\n"
        "• Alerts from one check are queued and sent rate-limited (never all "
        "at once).\n"
        "• Nothing repeats unless the condition clears and happens again — "
        "except Disconnect (every 60 min) and a 14h violation (every 30 min, "
        "up to 5 times).\n\n"

        "MESSAGE STYLE\n"
        "• “Hello. / Dear <name> / <the issue> / Thank you.” The driver is "
        "@-mentioned when their handle is known, and a PNG card "
        "(Break/Drive/Shift/Cycle gauges + name + truck + duty status) is "
        "attached.\n\n"

        "CURRENT SETTINGS\n"
        "check every 120 s · shift limit 14 h · low-hours driver 120/60/30 min · "
        "low-hours team 60/30 min · cycle 10/5 h · disconnect stale 30 min · "
        "disconnect re-alert 60 min · long on-duty 2 h · log image ON · "
        "disconnect alerts ON"
    )


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


def _driver_report(registry: GroupRegistry) -> str:
    records = registry.all()
    if not records:
        return "No driver groups are registered yet."
    lines = [f"Registered driver groups ({len(records)}):"]
    for record in sorted(records.values(), key=lambda item: item.get("driver_name", "")):
        lines.append(f"• {record.get('driver_name', '?')} → "
                     f"{record.get('title') or record.get('chat_id')}")
    return "\n".join(lines[:60])


def _groups_report(registry: GroupRegistry) -> str:
    groups = registry.known_groups()
    if not groups:
        return "I'm not in any groups yet. Add me to a driver's group as admin."
    lines = [f"Groups I can see ({len(groups)}):"]
    for cid, rec in sorted(groups.items(), key=lambda kv: kv[1].get("title", "")):
        who = registry.drivers_for_chat(cid)
        names = ", ".join(registry.driver_name(d) or d for d in who) if who else "— unassigned"
        lines.append(f"• {rec.get('title') or '(no title)'}\n   {cid}  →  {names}")
    return "\n".join(lines[:80])


def _unassigned_report(registry: GroupRegistry) -> str:
    groups = registry.unassigned_groups()
    if not groups:
        return "✅ Every group I'm in is assigned to a driver."
    lines = [f"Groups waiting to be assigned ({len(groups)}):"]
    for g in groups:
        lines.append(f"• {g['title'] or '(no title)'}\n   {g['chat_id']}  ({g['reason']})")
    lines.append("\nAssign with:  /assign <driver name> | <group-id>")
    return "\n".join(lines[:80])


def _roster_report(query: str, candidates: list[Candidate]) -> str:
    hits = _roster_search(query, candidates)
    if not hits:
        return f"No driver matching “{query}”. Roster has {len(candidates)} drivers."
    if len(hits) > 25:
        return f"“{query}” matches {len(hits)} drivers — be more specific."
    lines = [f"Matches for “{query}” ({len(hits)}):"]
    for c in sorted(hits, key=lambda c: c.name):
        bits = [c.name]
        if c.truck:
            bits.append(f"truck {c.truck}")
        if c.company:
            bits.append(c.company)
        bits.append(f"id …{_short_id(c.driver_id)}")
        lines.append("• " + "  |  ".join(bits))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command dispatch
# --------------------------------------------------------------------------- #
_ADMIN_CMDS = {
    "/drivers", "/groups", "/roster", "/assign", "/register", "/unassign",
    "/unregister", "/whois", "/unassigned", "/add", "/remove",
}


def _arg(text: str) -> str:
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _parse_assign_args(text: str, chat: dict) -> tuple[str, str | None, str | None]:
    """Return (query, chat_id, error)."""
    body = _arg(text)
    if not body:
        return "", None, "Usage: /assign <driver name> | <group-id>"

    chat_id: str | None = None
    if "|" in body:
        left, right = (p.strip() for p in body.rsplit("|", 1))
        if _looks_like_chat_id(right):
            body, chat_id = left, right
    if chat_id is None:
        parts = body.split()
        if parts and _looks_like_chat_id(parts[-1]):
            chat_id, body = parts[-1], " ".join(parts[:-1])
        elif parts and _looks_like_chat_id(parts[0]):
            chat_id, body = parts[0], " ".join(parts[1:])
    if chat_id is None and chat.get("type") in ("group", "supergroup"):
        chat_id = str(chat["id"])
    if chat_id is None:
        return body, None, ("Send the group id too:  /assign "
                            f"{body or '<driver name>'} | <group-id>   (see /groups)")
    return body.strip(), chat_id, None


def _handle_command(cmd: str, text: str, chat: dict, sender, registry: GroupRegistry,
                    candidates: list[Candidate], is_admin: bool) -> bool:
    """Return True if `cmd` is a recognised command (handled or refused)."""
    reply_to = str(chat["id"])

    def say(msg: str) -> None:
        sender.send_message(reply_to, msg)

    if cmd in ("/help", "/admin", "/start"):
        say(_help_text() if is_admin else "This bot is for authorised dispatchers only.")
        return True

    if cmd in ("/faq", "/logic", "/rules"):
        say(_faq_text())
        return True

    if not is_admin and cmd in _ADMIN_CMDS:
        say("This command is restricted to bot administrators.")
        return True

    if cmd in ("/coverage", "/status"):
        say(_coverage_report(registry))
        return True
    if cmd == "/drivers":
        say(_driver_report(registry))
        return True
    if cmd == "/groups":
        say(_groups_report(registry))
        return True
    if cmd == "/unassigned":
        say(_unassigned_report(registry))
        return True

    if cmd == "/roster":
        arg = _arg(text)
        say(_roster_report(arg, candidates) if arg
            else f"Usage: /roster <name or truck>. Roster has {len(candidates)} drivers.")
        return True

    if cmd == "/whois":
        parts = text.split()
        if len(parts) < 2 or not _looks_like_chat_id(parts[1]):
            say("Usage: /whois <group-id>   (ids from /groups)")
            return True
        cid = parts[1]
        who = registry.drivers_for_chat(cid)
        title = registry.group_title(cid) or "(unknown group)"
        if who:
            say(f"{title}\n{cid} → " + ", ".join(registry.driver_name(d) or d for d in who))
        else:
            say(f"{title}\n{cid} → not assigned to anyone.")
        return True

    if cmd in ("/assign", "/register"):
        query, chat_id, err = _parse_assign_args(text, chat)
        if err:
            say(err)
            return True
        hits = _roster_search(query, candidates)
        if not hits:
            say(f"No driver matching “{query}” on the roster. Try /roster {query}")
            return True
        if len(hits) != 1:
            names = "\n".join(f"  • {c.name}"
                              + (f" (truck {c.truck})" if c.truck else "")
                              for c in sorted(hits, key=lambda c: c.name)[:10])
            say(f"“{query}” matches {len(hits)} drivers — name one exactly:\n{names}\n"
                f"(for a co-driver group, /assign each name separately.)")
            return True
        driver = hits[0]
        title = registry.group_title(chat_id) or (
            (chat.get("title") or "") if str(chat.get("id")) == chat_id else "")
        registry.unblock(driver.driver_id)
        registry.register(driver.driver_id, chat_id, title, "manual", driver.name)
        registry.clear_pending(chat_id)
        log.info("assigned %s -> chat %s", driver.name, chat_id)
        say(f"✅ {driver.name} → {title or chat_id}\nHOS alerts will post there.")
        return True

    if cmd in ("/unassign", "/unregister", "/remove"):
        arg = _arg(text)
        if not arg:
            say("Usage: /unassign <driver name>")
            return True
        removed = []
        for c in candidates:
            if _name_matches(c.name, arg) or _name_matches(arg, c.name):
                if registry.unregister(c.driver_id):
                    removed.append(c.name)
                registry.block(c.driver_id)
        if removed:
            log.info("unassigned %s", removed)
            say(f"✅ Removed group for {', '.join(removed)}. Use /assign to re-add.")
        else:
            say(f"No registered driver matching “{arg}”.")
        return True

    if cmd == "/add":
        if chat.get("type") not in ("group", "supergroup"):
            say("Use /add inside the driver's group, or /assign <name> | <id> from here.")
            return True
        arg = _arg(text)
        if not arg:
            say("Usage: /add <driver name>  — sends that driver's alerts to this group.")
            return True
        added = []
        for c in candidates:
            if _name_matches(c.name, arg) or _name_matches(arg, c.name):
                registry.unblock(c.driver_id)
                registry.register(c.driver_id, chat["id"], chat.get("title") or "",
                                  "manual", c.name)
                added.append(c.name)
        registry.clear_pending(chat["id"])
        if added:
            log.info("added via command: %s to chat %s", added, chat["id"])
            say(f"✅ Registered {', '.join(added)} to this group. HOS alerts will be posted here.")
        else:
            say(f"No driver matching “{arg}” found on the roster.")
        return True

    return False


# --------------------------------------------------------------------------- #
# Update processing
# --------------------------------------------------------------------------- #
def _process_one(u: dict, sender, registry: GroupRegistry, candidates: list[Candidate],
                 *, admin_user_ids, reply: bool, now: str) -> tuple | None:
    """Handle a single update. Caller holds any lock and persists the offset."""
    chat = _chat_from_update(u)
    if not chat:
        return None
    cid = str(chat["id"])

    msg = u.get("message") or u.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    sender_id = (msg.get("from") or {}).get("id")
    is_admin = (not admin_user_ids) or (sender_id in admin_user_ids)

    if cmd and _handle_command(cmd, text, chat, sender, registry, candidates, is_admin):
        return None

    if chat.get("type") not in ("group", "supergroup"):
        return None

    title = chat.get("title") or ""
    if _bot_membership_change(u) == "left":
        registry.forget_group(cid)
        return None

    registry.record_group(cid, title, now)
    result: tuple | None = None

    # (a) Safe auto-register: the title must identify EXACTLY one driver.
    if not registry.drivers_for_chat(cid):
        did, how, reason = match_unique(title, candidates)
        if did and not registry.is_blocked(did):
            name = next((c.name for c in candidates if c.driver_id == did), "")
            if registry.register(did, cid, title, how, name):
                result = (name, cid, how, title)
                log.info("auto-registered %s -> %s [%s]", name, cid, title)
                if reply:
                    sender.send_message(
                        cid,
                        f"✅ This group is now linked to {name}. "
                        f"HOS alerts will be posted here.",
                    )
            registry.clear_pending(cid)
        else:
            registry.add_pending(cid, title, reason, now)
            log.info("group %s needs a human: %s (%s)", cid, title, reason)

    # (b) Capture the driver's Telegram @tag from their own messages.
    frm = msg.get("from") or {}
    if frm and not frm.get("is_bot"):
        sname = f"{frm.get('first_name', '')} {frm.get('last_name', '')}".strip()
        for did in registry.drivers_for_chat(cid):
            if _name_matches(registry.driver_name(did) or "", sname):
                if registry.set_tag(did, frm.get("username"), frm.get("id")):
                    handle = ("@" + frm["username"]) if frm.get("username") else f"id:{frm.get('id')}"
                    log.info("captured tag for %s -> %s", registry.driver_name(did), handle)
                break
    return result


def _process_batch(sender, registry: GroupRegistry, candidates: list[Candidate],
                   updates: list[dict], *, admin_user_ids=None, reply: bool = True,
                   lock=None) -> list[tuple]:
    guard = lock or contextlib.nullcontext()
    admin_ids = tuple(admin_user_ids or ())
    newly: list[tuple] = []
    max_id = registry.offset
    now = _now_iso()
    for u in updates:
        max_id = max(max_id, int(u.get("update_id", 0)))
        with guard:
            try:
                res = _process_one(u, sender, registry, candidates,
                                   admin_user_ids=admin_ids, reply=reply, now=now)
                if res:
                    newly.append(res)
            except Exception:  # one bad update must not stall the offset
                log.exception("error handling update %s", u.get("update_id"))
            registry.set_offset(max_id + 1)
            registry.save()
    return newly


def process_updates(sender, registry: GroupRegistry, candidates: list[Candidate],
                    *, admin_user_ids=None, reply: bool = True, lock=None) -> list[tuple]:
    """One sweep over pending updates (fetch + process). Advances the offset."""
    updates = sender.get_updates(registry.offset)
    if not updates:
        return []
    return _process_batch(sender, registry, candidates, updates,
                          admin_user_ids=admin_user_ids, reply=reply, lock=lock)


# --------------------------------------------------------------------------- #
# Live loop
# --------------------------------------------------------------------------- #
def run_command_loop(sender, registry: GroupRegistry, roster_cache, *,
                     admin_user_ids=None, lock=None,
                     stop_event: threading.Event | None = None,
                     poll_timeout: int = 25) -> None:
    """Long-poll Telegram and process commands/discovery until stopped.

    The sole getUpdates consumer while running. `roster_cache` is a
    roster_cache.RosterCache (memory-served, refreshed on its own TTL).
    """
    stop = stop_event or threading.Event()
    if not admin_user_ids:
        log.warning(
            "=" * 70 + "\n"
            "  ADMIN_TELEGRAM_USER_IDS is empty — every Telegram user is "
            "currently treated as an admin for /assign, /groups, /unassign, "
            "/whois, /unassigned, /add, /remove, /roster, /drivers.\n"
            + "=" * 70
        )
    if sender.set_my_commands(_BOT_COMMANDS):
        log.info("registered %d bot commands with Telegram", len(_BOT_COMMANDS))
    log.info("command loop started — long-poll %ds", poll_timeout)
    while not stop.is_set():
        started = time.monotonic()
        try:
            updates = sender.get_updates(registry.offset, timeout=poll_timeout)
        except Exception:
            log.exception("command loop: getUpdates failed")
            updates = []
        if not updates:
            # A real long-poll blocks ~poll_timeout seconds; a fast empty return
            # means the request errored — back off so we don't spin.
            if time.monotonic() - started < 2.0:
                stop.wait(5.0)
            continue
        try:
            newly = _process_batch(
                sender, registry, roster_cache.get(), updates,
                admin_user_ids=admin_user_ids, lock=lock,
            )
            for name, chat_id, how, title in newly:
                log.info("registered group for %s (by %s): %s [%s]", name, how, title, chat_id)
        except Exception:
            log.exception("command loop: batch processing failed")
    log.info("command loop stopped")
