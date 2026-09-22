"""Small helpers shared by the dashboard views."""

from __future__ import annotations

import csv
import io
import re

from flask import Response, request, url_for

from src.messages import KIND_LABELS, LANGUAGES as _LANGUAGE_NAMES

#: (code, label) for the alert-type checkboxes, in display order.
ALERT_KINDS = [
    ("low_hours", "Low hours"),
    ("cycle", "Cycle low"),
    ("shift_violation", "Shift violation"),
    ("disconnect", "Disconnect"),
    ("on_duty", "Long On Duty"),
    ("off_duty_checkin", "Off-duty check-in"),
]
assert [k for k, _ in ALERT_KINDS] == list(KIND_LABELS)

LANGUAGES = [("en", "English"), ("ru", "Russian — Русский"),
             ("uz", "Uzbek — O'zbek"), ("es", "Spanish — Español")]
assert [c for c, _ in LANGUAGES] == list(_LANGUAGE_NAMES)

_CHAT_ID = re.compile(r"^-?\d{5,}$")
_TRAILING_ID = re.compile(r"\((-?\d{5,})\)\s*$")


def safe_next(default_endpoint: str, **values) -> str:
    """Where to go back to after a form: the page it was posted from, if that
    is a path on this site; never an absolute or protocol-relative URL."""
    nxt = (request.form.get("next") or request.args.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return url_for(default_endpoint, **values)


def group_options(registry) -> list[dict]:
    rows = [{"chat_id": cid, "title": rec.get("title") or ""}
            for cid, rec in registry.known_groups().items()]
    rows.sort(key=lambda g: (g["title"] or "~").lower())
    return rows


def resolve_group(registry, text: str) -> tuple[str | None, str | None]:
    """Turn what an operator typed into a chat id.

    Accepts a picked list entry ("Title (-100123)"), a bare chat id, or a group
    title typed out in full. Returns (chat_id, None) or (None, error). Empty
    input is (None, None) — "no group".
    """
    value = (text or "").strip()
    if not value:
        return None, None
    m = _TRAILING_ID.search(value)
    if m:
        return m.group(1), None
    if _CHAT_ID.match(value):
        return value, None
    hits = registry.find_groups(value)
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, (f"Two or more groups are called “{value}” — pick the one "
                      f"with the right chat id from the list.")
    return None, (f"No group called “{value}”. Add the bot to that Telegram group and "
                  f"send any message there — it then appears in the list.")


def csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        "﻿" + buf.getvalue(),  # BOM so Excel opens UTF-8 names correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
