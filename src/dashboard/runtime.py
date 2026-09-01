"""RuntimeContext — the single shared handle between the dashboard's Flask
app (request threads) and the scheduler's background thread.

Created once in main.py's run_dashboard(). config.yaml is re-read on every
current_config() call (cheap — just YAML+env parsing) so dashboard edits are
picked up by both the dashboard itself and, via run_forever's
config_loader=load_config, the scheduler — without a restart.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from src.activity_log import ActivityLog
from src.config import Config, load_config
from src.registry import GroupRegistry
from src.roster_cache import RosterCache
from src.state import AlertState


@dataclass
class RuntimeContext:
    config_path: str
    env_path: str
    registry: GroupRegistry
    state: AlertState
    activity_log: ActivityLog
    roster_cache: RosterCache | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_cycle_stats: object | None = None
    last_cycle_at: datetime | None = None

    def current_config(self) -> Config:
        return load_config(self.config_path, self.env_path)
