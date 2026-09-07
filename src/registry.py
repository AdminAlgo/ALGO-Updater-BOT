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
    company: str | None = None


def _narrow_by_company(t: str, candidates: list[Candidate]) -> list[Candidate]:
    """If the (normalized) title names exactly one company present among the
    candidates, narrow matching to that company's drivers only.

    Truck numbers are assigned per-company, so the same number (e.g. "708")
    can legitimately belong to a different driver at a different company. A
    group title that says which company it belongs to (as ours do — e.g.
    "#708 | Bakhodir Saidov | ZOHA LLC") must not let that number cross-match
    a same-numbered truck at an unrelated company. Titles that don't mention
    a company (or can't be pinned to exactly one) fall back to the full pool,
    same as before.
    """
    companies = {c.company for c in candidates if c.company}
    if len(companies) < 2:
        return candidates  # nothing to disambiguate
    hit = {comp for comp in companies if normalize_name(comp) in t}
    if len(hit) == 1:
        only = hit.pop()
        return [c for c in candidates if c.company == only]
    return candidates


def match_title(title: str, candidates: list[Candidate]) -> tuple[str | None, str | None]:
    """Match a group title to a driver. Returns (driver_id, matched_on) or (None, None).

    Priority: full name (substring) → truck number (whole token) → username.
    """
    t = normalize_name(title)
    if not t:
        return None, None
    candidates = _narrow_by_company(t, candidates)

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
    candidates = _narrow_by_company(t, candidates)
    by_name = [(c.driver_id, "name") for c in candidates if c.name and normalize_name(c.name) in t]
    if by_name:
        return by_name
    by_truck = [(c.driver_id, "truck") for c in candidates
                if c.truck and re.search(rf"(?<!\d){re.escape(str(c.truck))}(?!\d)", t)]
    if by_truck:
        return by_truck
    return [(c.driver_id, "username") for c in candidates
            if c.username and normalize_name(c.username) in t]


def match_unique(title: str, candidates: list[Candidate]) -> tuple[str | None, str | None, str]:
    """Strict match for safe auto-registration at scale.

    Only returns a driver when the title identifies EXACTLY ONE of them, in a
    single tier (name / truck / username). Anything ambiguous (two drivers named
    in the title, two "John Smith"s on the roster) returns (None, None, reason)
    so a human confirms it instead. The third value is a short reason string for
    logging / the review queue.
    """
    t = normalize_name(title)
    if not t:
        return None, None, "empty title"
    candidates = _narrow_by_company(t, candidates)

    by_name = [c for c in candidates if c.name and normalize_name(c.name) in t]
    if len(by_name) == 1:
        return by_name[0].driver_id, "name", "unique name"
    if len(by_name) > 1:
        names = ", ".join(sorted(c.name for c in by_name))
        return None, None, f"ambiguous — title names {len(by_name)} drivers ({names})"

    by_truck = [c for c in candidates
                if c.truck and re.search(rf"(?<!\d){re.escape(str(c.truck))}(?!\d)", t)]
    if len(by_truck) == 1:
        return by_truck[0].driver_id, "truck", "unique truck number"
    if len(by_truck) > 1:
        return None, None, f"ambiguous — truck number matches {len(by_truck)} drivers"

    by_user = [c for c in candidates if c.username and normalize_name(c.username) in t]
    if len(by_user) == 1:
        return by_user[0].driver_id, "username", "unique username"

    return None, None, "no confident match"


class GroupRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._offset: int = 0
        self._drivers: dict[str, dict] = {}
        # Drivers explicitly removed via /remove — auto-registration skips them
        # until /add re-enables (otherwise a matching group title would silently
        # re-register them on the next message).
        self._blocked: set[str] = set()
        # Every group/supergroup the bot has been added to or seen a message in,
        # keyed by str(chat_id): {"title", "first_seen", "last_seen"}. This is the
        # pool of assignable targets — independent of whether a driver is matched.
        self._groups: dict[str, dict] = {}
        # Groups the bot joined that could not be auto-matched to exactly one
        # driver: {chat_id: {"title", "reason", "seen"}}. Surfaced by /unassigned
        # and the dashboard for a human to resolve.
        self._pending: dict[str, dict] = {}
        if self._path and self._path.exists():
            self.load()

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8")) or {}
            self._offset = int(data.get("offset", 0))
            self._drivers = data.get("drivers", {}) or {}
            self._blocked = set(data.get("blocked", []) or [])
            self._groups = data.get("groups", {}) or {}
            self._pending = data.get("pending", {}) or {}
        except (json.JSONDecodeError, OSError, ValueError):
            self._offset, self._drivers, self._blocked = 0, {}, set()
            self._groups, self._pending = {}, {}

    def save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.tmp")
            temporary.write_text(
                json.dumps({"offset": self._offset, "drivers": self._drivers,
                            "blocked": sorted(self._blocked),
                            "groups": self._groups, "pending": self._pending},
                           indent=2),
                "utf-8",
            )
            temporary.replace(self._path)

    # --- known groups (assignable targets) --- #
    def record_group(self, chat_id, title: str, when: str) -> None:
        """Note that the bot can see this group. Idempotent; refreshes title."""
        cid = str(chat_id)
        rec = self._groups.get(cid)
        if rec is None:
            self._groups[cid] = {"title": title or "", "first_seen": when, "last_seen": when}
        else:
            if title:
                rec["title"] = title
            rec["last_seen"] = when

    def known_groups(self) -> dict[str, dict]:
        return dict(self._groups)

    def group_title(self, chat_id) -> str | None:
        rec = self._groups.get(str(chat_id))
        return rec.get("title") if rec else None

    def forget_group(self, chat_id) -> None:
        """Bot was removed from the group — drop it from the assignable pool."""
        cid = str(chat_id)
        self._groups.pop(cid, None)
        self._pending.pop(cid, None)

    # --- pending (needs-a-human) queue --- #
    def add_pending(self, chat_id, title: str, reason: str, when: str) -> None:
        if self.driver_for_chat(chat_id) or self.drivers_for_chat(chat_id):
            return  # already assigned — nothing pending
        self._pending[str(chat_id)] = {"title": title or "", "reason": reason, "seen": when}

    def clear_pending(self, chat_id) -> None:
        self._pending.pop(str(chat_id), None)

    def pending(self) -> dict[str, dict]:
        return dict(self._pending)

    def unassigned_groups(self) -> list[dict]:
        """Known groups with no driver assigned (includes the pending reason)."""
        out: list[dict] = []
        for cid, rec in list(self._groups.items()):
            if self.drivers_for_chat(cid):
                continue
            p = self._pending.get(cid) or {}
            out.append({"chat_id": cid, "title": rec.get("title") or "",
                        "reason": p.get("reason", "not matched yet")})
        return out

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
    # Readers iterate over a snapshot copy: the scheduler thread reads the
    # registry while the command-loop thread may be mutating it, and a plain
    # dict iteration would raise "changed size during iteration".
    def driver_for_chat(self, chat_id) -> str | None:
        for did, rec in list(self._drivers.items()):
            if str(rec.get("chat_id")) == str(chat_id):
                return did
        return None

    def drivers_for_chat(self, chat_id) -> list[str]:
        """All driver ids registered to a chat (co-driver / team groups)."""
        return [did for did, rec in list(self._drivers.items())
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
        for rec in list(self._drivers.values()):
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


def discover_groups(sender, registry: GroupRegistry, candidates: list[Candidate],
                    reply: bool = True, admin_user_ids=None) -> list[tuple]:
    """One-shot sweep over pending Telegram updates (commands + group discovery).

    Kept for the ``--discover`` CLI and any caller that wants a single pass. The
    live service uses ``commands.run_command_loop`` instead. Delegates to
    ``commands.process_updates`` (deferred import avoids a cycle).
    """
    from .commands import process_updates

    return process_updates(
        sender, registry, candidates,
        admin_user_ids=admin_user_ids, reply=reply,
    )
