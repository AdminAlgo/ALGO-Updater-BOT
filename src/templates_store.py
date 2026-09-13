"""Message-template store for the Warnings/Broadcast composer (upgrade spec §3.3).

``data/message_templates.json``::

    {"templates": [{"id": "...", "name": "...", "created": "...",
                    "text": {"en": "...", "ru": "...", "uz": "...", "es": "..."}}]}

A template with only an "en" string is fine — missing languages fall back to
English at render time (never send a blank message).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from .storage import atomic_write_json, read_json

LANGUAGES = ("en", "ru", "uz", "es")


class TemplateStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._templates: list[dict] = []
        if self._path and self._path.exists():
            self.load()

    def load(self) -> None:
        if not self._path:
            return
        data = read_json(self._path, default={}) or {}
        self._templates = data.get("templates", []) or []

    def save(self) -> None:
        if not self._path:
            return
        atomic_write_json(self._path, {"templates": self._templates})

    def all(self) -> list[dict]:
        return list(self._templates)

    def get(self, template_id: str) -> dict | None:
        return next((t for t in self._templates if t.get("id") == template_id), None)

    def add(self, name: str, text: dict[str, str]) -> dict:
        template = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "created": datetime.now(timezone.utc).isoformat(),
            "text": {k: v.strip() for k, v in text.items() if v and v.strip()},
        }
        self._templates.append(template)
        self.save()
        return template

    def update(self, template_id: str, name: str | None = None,
               text: dict[str, str] | None = None) -> bool:
        t = self.get(template_id)
        if not t:
            return False
        if name:
            t["name"] = name
        if text is not None:
            t["text"] = {k: v.strip() for k, v in text.items() if v and v.strip()}
        self.save()
        return True

    def delete(self, template_id: str) -> bool:
        before = len(self._templates)
        self._templates = [t for t in self._templates if t.get("id") != template_id]
        changed = len(self._templates) != before
        if changed:
            self.save()
        return changed

    @staticmethod
    def render(template: dict, language: str) -> str:
        """The template's text in ``language``, falling back to English."""
        text = template.get("text") or {}
        return text.get(language) or text.get("en") or ""
