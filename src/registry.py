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


def dedupe_drivers(rows, seen: dict[str, str], company: str) -> tuple[list, list[str]]:
    """Keep one row per driver_id ACROSS companies; the first company wins.

    Two config entries sitting on the same ELD account — a carrier added a
    second time under a new name, or the same API key pasted into both — return
    the SAME drivers, and every repeat then reads as a second person: /assign
    refuses with "matches 2 drivers", best_match calls every group title
    ambiguous so nobody gets linked, and the rules evaluate one driver twice.

    `seen` maps driver_id -> the company that claimed it and is updated in
    place, so a caller walking companies in config order carries it across the
    whole roster. Returns (rows to keep, one report line per dropped repeat).
    Works on anything with .driver_id and .name — Candidate or DriverSnapshot.
    """
    kept: list = []
    dupes: list[str] = []
    for row in rows:
        owner = seen.get(row.driver_id)
        if owner is not None:
            dupes.append(f"{row.name} ({company} duplicates {owner})")
            continue
        seen[row.driver_id] = company
        kept.append(row)
    return kept, dupes


def unique_drivers(rows) -> list:
    """One row per driver_id, order preserved — for a flat roster whose rows
    already carry their own company. Same protection as `dedupe_drivers`, for
    callers that are handed a finished roster instead of building one."""
    seen: set[str] = set()
    out: list = []
    for row in rows:
        if row.driver_id in seen:
            continue
        seen.add(row.driver_id)
        out.append(row)
    return out


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


# --------------------------------------------------------------------------- #
# Zero-touch matching engine
# --------------------------------------------------------------------------- #
# Group titles in the field never look like the roster: the name order is
# flipped ("Karimov Aziz"), a middle name is dropped ("Abib Ali Mohamed" titled
# "Abib Mohamed"), the title is written in Cyrillic, or the name is glued to
# punctuation ("Aziz.Karimov"). The substring test in match_unique misses every
# one of those, which is why groups the bot could already see stayed unlinked
# and their drivers got no alerts at all.
#
# Everything below compares *phonetic token keys* instead of raw substrings, so
# all of those link themselves with nobody typing a command.

_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Uzbek / Kazakh Cyrillic extras seen on driver groups
    "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h", "ә": "a", "і": "i", "ң": "n",
    "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "җ": "j", "ҷ": "j",
}

# Spellings that mean the same sound across ELD records, Telegram titles and
# transliterated Cyrillic: Khasanov == Hasanov, Yuldashev == Iuldashev,
# Sherzod == Serzod. Collapsing them makes the two spellings compare equal.
_SOUND_RULES = (("kh", "h"), ("zh", "j"), ("ts", "c"), ("ch", "c"), ("sh", "s"),
                ("yo", "io"), ("yu", "iu"), ("ya", "ia"), ("ph", "f"), ("ck", "k"))

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-zЀ-ӿ]+")


def _translit(value: str) -> str:
    return "".join(_CYR2LAT.get(ch, ch) for ch in value)


def token_key(token: str) -> str:
    """Reduce one word to a spelling-insensitive key for comparison.

    "Каримов" and "Karimov" collapse to the same key, so a Cyrillic group title
    still matches a Latin roster name. Same for the transliteration variants
    that differ only in how a sound was spelled (Khasanov / Hasanov).
    """
    t = _translit((token or "").lower())
    t = re.sub(r"[^a-z0-9]", "", t)
    if t.isdigit():
        return t.lstrip("0") or t  # "#0118" and "118" are the same truck
    for a, b in _SOUND_RULES:
        t = t.replace(a, b)
    t = t.replace("y", "i")           # Yakubov == Iakubov
    return re.sub(r"(.)\1+", r"\1", t)  # doubled: Abbos == Abos


def title_keys(value: str) -> set[str]:
    """Every word of a title/name as a comparable key (order-independent)."""
    return {k for k in (token_key(t) for t in _TOKEN_SPLIT.split(value or "")) if k}


def _name_keys(name: str) -> set[str]:
    """Name words long enough to identify someone (drops initials, jr, …)."""
    return {k for k in title_keys(name) if len(k) >= 3}


@dataclass(frozen=True)
class GroupMatch:
    """Who a group belongs to, and how sure we are.

    confidence drives the routing decision:
      "high"   — link automatically; HOS alerts start flowing to this group.
      "medium" — a real lead but not proof, so it is offered in the dashboard
                 for a one-click confirm instead of routing a driver's hours.
      "none"   — nothing confident; reason says what stopped it.
    """
    driver_id: str | None
    matched_on: str
    confidence: str
    reason: str

    @property
    def linkable(self) -> bool:
        return self.driver_id is not None and self.confidence == "high"


def best_match(title: str, candidates: list[Candidate]) -> GroupMatch:
    """Identify the one driver a group title belongs to.

    Ambiguity always loses: if a title fits two drivers, nobody is linked and
    the reason records why — misrouting one driver's HOS alerts to another is
    worse than a group staying unlinked for another day.
    """
    tkeys = title_keys(title)
    if not tkeys:
        return GroupMatch(None, "", "none", "empty title")

    digits = {k for k in tkeys if k.isdigit()}
    freq: dict[str, int] = {}
    for c in candidates:
        for k in _name_keys(c.name):
            freq[k] = freq.get(k, 0) + 1

    scored = []
    for c in candidates:
        hits = _name_keys(c.name) & tkeys
        truck = bool(c.truck) and token_key(str(c.truck)) in digits
        scored.append((c, len(hits), truck, hits))

    def _names(rows) -> str:
        return ", ".join(sorted(r[0].name for r in rows))

    # Tier 1 — two or more name words match, in any order. Covers a flipped
    # name, a dropped middle name, Cyrillic and punctuation all at once.
    tier = [s for s in scored if s[1] >= 2]
    if tier:
        top = max(s[1] for s in tier)
        best = [s for s in tier if s[1] == top]
        if len(best) > 1:
            with_truck = [s for s in best if s[2]]
            if len(with_truck) == 1:  # the truck number breaks the tie
                best = with_truck
        if len(best) == 1:
            return GroupMatch(best[0][0].driver_id, "name", "high",
                              f"{top} name words match")
        return GroupMatch(None, "", "none",
                          f"ambiguous — title names {len(best)} drivers ({_names(best)})")

    # Tier 2 — one name word plus the truck number.
    tier = [s for s in scored if s[1] == 1 and s[2]]
    if len(tier) == 1:
        return GroupMatch(tier[0][0].driver_id, "name+truck", "high",
                          "name word + truck number")
    if len(tier) > 1:
        return GroupMatch(None, "", "none",
                          f"ambiguous — name + truck fits {len(tier)} drivers ({_names(tier)})")

    # Tier 3 — the truck number on its own.
    tier = [s for s in scored if s[2]]
    if len(tier) == 1:
        return GroupMatch(tier[0][0].driver_id, "truck", "high", "unique truck number")
    if len(tier) > 1:
        return GroupMatch(None, "", "none",
                          f"ambiguous — truck number matches {len(tier)} drivers")

    # Tier 4 — the driver's Telegram username in the title.
    tier = [s for s in scored if s[0].username and token_key(s[0].username) in tkeys]
    if len(tier) == 1:
        return GroupMatch(tier[0][0].driver_id, "username", "high", "unique username")

    # Tier 5 — a single name word that belongs to exactly one driver in the
    # whole fleet. A good lead, but one word is not proof of identity.
    tier = [s for s in scored
            if s[1] == 1 and all(freq.get(k, 0) == 1 and len(k) >= 4 for k in s[3])]
    if len(tier) == 1:
        word = sorted(tier[0][3])[0]
        return GroupMatch(tier[0][0].driver_id, "name", "medium",
                          f"only the name word {word} matches — confirm it")

    return GroupMatch(None, "", "none", "no confident match")


def match_sender(sender_name: str, candidates: list[Candidate]) -> GroupMatch:
    """Identify the roster driver who just wrote, from their Telegram name.

    This is what links groups whose title says nothing useful ("ELD", "Truck",
    "Work") — the driver writing in their own group identifies it for us. Two
    matching name words are required, so a one-word Telegram handle can never
    claim a group.
    """
    skeys = title_keys(sender_name)
    if len(skeys) < 2:
        return GroupMatch(None, "", "none", "sender name too short to be sure")
    rows = [(c, len(_name_keys(c.name) & skeys)) for c in candidates]
    rows = [r for r in rows if r[1] >= 2]
    if not rows:
        return GroupMatch(None, "", "none", "sender is not on the roster")
    top = max(n for _, n in rows)
    winners = [c for c, n in rows if n == top]
    if len(winners) == 1:
        return GroupMatch(winners[0].driver_id, "sender", "high",
                          "the driver wrote in this group")
    return GroupMatch(None, "", "none", "sender name fits more than one driver")


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
    def add_pending(self, chat_id, title: str, reason: str, when: str,
                    suggested_id: str | None = None, suggested_name: str = "",
                    confidence: str = "none") -> None:
        """Queue a group nobody could be linked to, with its best lead if any.

        ``suggested_id`` carries a medium-confidence match — good enough to show
        in the dashboard for a one-click confirm, not good enough to route a
        driver's HOS alerts on by itself.
        """
        if self.driver_for_chat(chat_id) or self.drivers_for_chat(chat_id):
            return  # already assigned — nothing pending
        self._pending[str(chat_id)] = {
            "title": title or "", "reason": reason, "seen": when,
            "suggested_id": suggested_id, "suggested_name": suggested_name,
            "confidence": confidence,
        }

    def suggestions(self) -> list[dict]:
        """Unlinked groups that have a driver suggested — the "Link all" list.

        Skips a suggestion whose driver is already linked to another group or
        was explicitly removed, so one click can never steal a working link.
        """
        out: list[dict] = []
        for cid, rec in list(self._pending.items()):
            did = rec.get("suggested_id")
            if not did or self.is_blocked(did) or self.is_registered(did):
                continue
            out.append({"chat_id": cid, "title": rec.get("title") or "",
                        "driver_id": did, "driver_name": rec.get("suggested_name") or did,
                        "reason": rec.get("reason", "")})
        return out

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
                        "reason": p.get("reason", "not matched yet"),
                        "suggested_id": p.get("suggested_id"),
                        "suggested_name": p.get("suggested_name") or "",
                        "confidence": p.get("confidence", "none")})
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

        Preserves any captured Telegram tag (tg_username / tg_user_id) and any
        dashboard-managed settings (dispatch_chat_id / language / disabled_kinds)
        so re-discovery (title match) never wipes them out.
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
            "dispatch_chat_id": prev.get("dispatch_chat_id"),
            "language": prev.get("language") or "en",
            "disabled_kinds": list(prev.get("disabled_kinds") or []),
        }
        return changed

    # --- dashboard-managed per-driver settings (upgrade spec §3.1) --------- #
    # A record's dispatch_chat_id / language / disabled_kinds may be absent on
    # old registry.json entries — every reader below defaults via .get(), so no
    # migration step is needed; old records keep working unmodified.
    def dispatch_chat_for(self, driver_id: str) -> str | None:
        rec = self._drivers.get(driver_id)
        return (rec.get("dispatch_chat_id") if rec else None) or None

    def language_for(self, driver_id: str) -> str:
        rec = self._drivers.get(driver_id)
        return (rec.get("language") if rec else None) or "en"

    def disabled_kinds_for(self, driver_id: str) -> list[str]:
        rec = self._drivers.get(driver_id)
        return list(rec.get("disabled_kinds") or []) if rec else []

    _UNSET = object()

    def update_driver_settings(self, driver_id: str, *, driver_name: str | None = None,
                               chat_id: str | None = None,
                               dispatch_chat_id=_UNSET,
                               language: str | None = None,
                               disabled_kinds: list[str] | None = None) -> None:
        """Create-or-update the fields the Drivers-page Edit modal manages.

        ``dispatch_chat_id`` defaults to a sentinel so "not passed" (leave
        unchanged) is distinguishable from "passed as None/empty" (clear it).
        Creates a bare record if this driver has no group yet (Phase 1's "Add
        driver" is assigning an existing roster driver, not creating one).
        """
        rec = self._drivers.setdefault(driver_id, {
            "chat_id": "", "title": "", "matched_on": "manual",
            "driver_name": driver_name or driver_id,
            "tg_username": None, "tg_user_id": None,
            "dispatch_chat_id": None, "language": "en", "disabled_kinds": [],
        })
        if driver_name:
            rec["driver_name"] = driver_name
        if chat_id is not None:
            rec["chat_id"] = str(chat_id)
        if dispatch_chat_id is not GroupRegistry._UNSET:
            rec["dispatch_chat_id"] = str(dispatch_chat_id) if dispatch_chat_id else None
        if language is not None:
            rec["language"] = language
        if disabled_kinds is not None:
            rec["disabled_kinds"] = list(disabled_kinds)

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
