"""Shared atomic JSON file persistence.

Same tmp-then-replace pattern already used independently by AlertState
(state.py) and GroupRegistry (registry.py): write to a sibling ``.name.tmp``
file, then atomically replace the real path, so a crash mid-write never
leaves a corrupt/partial file behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2), "utf-8")
    temporary.replace(path)


def read_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
