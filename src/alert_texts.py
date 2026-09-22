"""Operator-edited alert wording (dashboard → Warnings → Alert texts).

``DATA_DIR/alert_texts.json`` = ``{kind: {language: text}}``. Only the texts
someone actually changed are stored; every other kind/language keeps the
built-in wording in messages.py. Loading (or saving) installs the set into
messages.set_overrides(), which is what the rules render from — the scheduler
and the dashboard share one process, so an edit applies on the next cycle.
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import messages
from .storage import atomic_write_json, read_json


class AlertTextStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._texts: dict[str, dict[str, str]] = {}
        if self._path is not None:
            self._texts = read_json(self._path, default={}) or {}
        messages.set_overrides(self._texts)

    def all(self) -> dict[str, dict[str, str]]:
        return {k: dict(v) for k, v in self._texts.items()}

    def save_kind(self, kind: str, texts: dict[str, str]) -> list[str]:
        """Validate and store one kind's texts. A text equal to the built-in
        wording (or blank) is dropped so it keeps following future defaults.
        Returns problems; nothing is saved unless the list is empty."""
        if kind not in messages.TEMPLATES:
            return [f"unknown alert kind {kind!r}"]
        problems: list[str] = []
        keep: dict[str, str] = {}
        for lang in messages.LANGUAGES:
            text = (texts.get(lang) or "").replace("\r\n", "\n").strip()
            if not text or text == messages.default_template(kind, lang).strip():
                continue
            for p in messages.template_problems(kind, text):
                problems.append(f"{messages.LANGUAGES[lang]}: {p}")
            keep[lang] = text
        if problems:
            return problems
        with self._lock:
            if keep:
                self._texts[kind] = keep
            else:
                self._texts.pop(kind, None)
            self._persist_locked()
        return []

    def reset_kind(self, kind: str) -> None:
        with self._lock:
            self._texts.pop(kind, None)
            self._persist_locked()

    def _persist_locked(self) -> None:
        if self._path is not None:
            atomic_write_json(self._path, self._texts)
        messages.set_overrides(self._texts)
