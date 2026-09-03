"""Alert state — prevents the bot from spamming a group on every poll cycle.

Tracks, per driver:
  - low_hours_fired:        which minute-thresholds have already alerted, per
                            audience (driver_group / team_group). Cleared when
                            the driver's timers recover (a new shift / reset),
                            so the next shift can alert again.
  - shift_violation_active: whether a 14h violation alert has already fired for
                            the current violation episode. Cleared when shift
                            time recovers above zero.
  - last_disconnect_iso:    when the last "disconnected" alert was sent, so the
                            rules can honor `disconnect_realert_minutes`.
                            Cleared on reconnect so a fresh disconnect alerts
                            immediately.

State is kept in memory and optionally persisted to a JSON file so a restart
doesn't re-spam. It is intentionally simple and JSON-serializable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _empty_record() -> dict[str, Any]:
    return {
        "low_hours_fired": {"driver_group": [], "team_group": []},
        "cycle_fired": {"driver_group": [], "team_group": []},
        "shift_violation_active": False,
        "shift_violation_last_sent_iso": None,
        "shift_violation_resend_count": 0,
        "last_disconnect_iso": None,
        "on_duty_since": None,        # when the current On-Duty episode began
        "on_duty_alerted": False,     # welfare check already sent this episode
    }


class AlertState:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._drivers: dict[str, dict[str, Any]] = {}
        if self._path and self._path.exists():
            self.load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            self._drivers = json.loads(self._path.read_text("utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            self._drivers = {}

    def save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        temporary.write_text(json.dumps(self._drivers, indent=2), "utf-8")
        temporary.replace(self._path)

    def _rec(self, driver_id: str) -> dict[str, Any]:
        return self._drivers.setdefault(driver_id, _empty_record())

    # ------------------------------------------------------------------ #
    # Low-hours thresholds
    # ------------------------------------------------------------------ #
    def low_hours_already_fired(self, driver_id: str, audience: str, threshold: int) -> bool:
        return threshold in self._rec(driver_id)["low_hours_fired"].get(audience, [])

    def mark_low_hours_fired(self, driver_id: str, audience: str, threshold: int) -> None:
        fired = self._rec(driver_id)["low_hours_fired"].setdefault(audience, [])
        if threshold not in fired:
            fired.append(threshold)

    def reset_low_hours(self, driver_id: str) -> None:
        self._rec(driver_id)["low_hours_fired"] = {"driver_group": [], "team_group": []}

    # ------------------------------------------------------------------ #
    # 70-hour Cycle thresholds (A1) — same fire-once-per-threshold shape as
    # low-hours, tracked separately since it's a distinct timer/rule.
    # ------------------------------------------------------------------ #
    def cycle_already_fired(self, driver_id: str, audience: str, threshold: int) -> bool:
        rec = self._rec(driver_id).setdefault("cycle_fired", {"driver_group": [], "team_group": []})
        return threshold in rec.get(audience, [])

    def mark_cycle_fired(self, driver_id: str, audience: str, threshold: int) -> None:
        rec = self._rec(driver_id).setdefault("cycle_fired", {"driver_group": [], "team_group": []})
        fired = rec.setdefault(audience, [])
        if threshold not in fired:
            fired.append(threshold)

    def reset_cycle(self, driver_id: str) -> None:
        self._rec(driver_id)["cycle_fired"] = {"driver_group": [], "team_group": []}

    # ------------------------------------------------------------------ #
    # Shift violation episode — fires immediately, then resends on a timer
    # while the driver is still in violation (see shift_violation_due).
    # ------------------------------------------------------------------ #
    def shift_violation_active(self, driver_id: str) -> bool:
        return bool(self._rec(driver_id)["shift_violation_active"])

    def shift_violation_resend_count(self, driver_id: str) -> int:
        return int(self._rec(driver_id).get("shift_violation_resend_count", 0))

    def shift_violation_due(self, driver_id: str, resend_minutes: int, now: datetime) -> bool:
        """True if enough time has passed since the last violation alert to resend."""
        last = self._rec(driver_id).get("shift_violation_last_sent_iso")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now - last_dt).total_seconds() >= resend_minutes * 60

    def mark_shift_violation_sent(self, driver_id: str, now: datetime, is_resend: bool) -> None:
        rec = self._rec(driver_id)
        rec["shift_violation_active"] = True
        rec["shift_violation_last_sent_iso"] = now.isoformat()
        if is_resend:
            rec["shift_violation_resend_count"] = rec.get("shift_violation_resend_count", 0) + 1

    def clear_shift_violation(self, driver_id: str) -> None:
        rec = self._rec(driver_id)
        rec["shift_violation_active"] = False
        rec["shift_violation_last_sent_iso"] = None
        rec["shift_violation_resend_count"] = 0

    # ------------------------------------------------------------------ #
    # Disconnect re-alert throttle
    # ------------------------------------------------------------------ #
    def disconnect_due(self, driver_id: str, realert_minutes: int, now: datetime) -> bool:
        """True if no disconnect alert has fired recently enough to suppress one."""
        last = self._rec(driver_id)["last_disconnect_iso"]
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now - last_dt).total_seconds() >= realert_minutes * 60

    def mark_disconnect_alert(self, driver_id: str, now: datetime) -> None:
        self._rec(driver_id)["last_disconnect_iso"] = now.isoformat()

    def clear_disconnect(self, driver_id: str) -> None:
        self._rec(driver_id)["last_disconnect_iso"] = None

    # ------------------------------------------------------------------ #
    # Prolonged On-Duty welfare check
    # ------------------------------------------------------------------ #
    def on_duty_since(self, driver_id: str):
        return self._rec(driver_id).get("on_duty_since")

    def set_on_duty_since(self, driver_id: str, iso: str) -> None:
        self._rec(driver_id)["on_duty_since"] = iso

    def on_duty_alerted(self, driver_id: str) -> bool:
        return bool(self._rec(driver_id).get("on_duty_alerted"))

    def set_on_duty_alerted(self, driver_id: str, value: bool) -> None:
        self._rec(driver_id)["on_duty_alerted"] = bool(value)

    def clear_on_duty(self, driver_id: str) -> None:
        rec = self._rec(driver_id)
        rec["on_duty_since"] = None
        rec["on_duty_alerted"] = False
