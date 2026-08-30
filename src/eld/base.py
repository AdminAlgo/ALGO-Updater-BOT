"""ELD provider interface and the normalized driver snapshot model.

Both providers (Factor / DriveHOS today, Leader later) implement ``ELDProvider``
and return the SAME normalized objects, so rules.py and scheduler.py can treat
every provider identically regardless of the underlying API shape.

What's normalized here:
  - HOS timers are stored as SECONDS REMAINING (the DriveHOS native unit).
  - Duty status keeps the provider code AND a human label (see DUTY_STATUS_LABELS).
  - Connection state captures whether a vehicle is paired, its motion/OFFLINE
    status, and the last telemetry time — the raw facts the disconnect rule needs.

The disconnect *decision* (OFFLINE or stale-while-active) lives in rules.py; this
module only exposes the facts plus small helpers.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

# DriveHOS / Factor duty-status codes -> human labels.
# connection_required_statuses in config.yaml use the labels on the right.
DUTY_STATUS_LABELS: dict[str, str] = {
    "DS_D": "Driving",
    "DS_ON": "On Duty",
    "DS_YM": "Yard Move",
    "DS_SB": "Sleeper",
    "DS_OFF": "Off Duty",
    "DS_PC": "Personal Conveyance",
}

# Vehicle telemetry status that explicitly means "not reporting".
VEHICLE_OFFLINE = "OFFLINE"


class ELDError(Exception):
    """Raised when an ELD provider call fails (network, auth, or bad payload)."""


class Unauthorized(ELDError):
    """Raised when a provider call gets HTTP 401 — almost always a wrong or
    revoked Provider/Company API key, not a transient failure."""


def normalize_name(value: str) -> str:
    """Lowercase, strip, and collapse internal whitespace for name matching.

    "  Abib   Ali Mohamed " -> "abib ali mohamed"
    """
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def label_for_status(code: str | None) -> str:
    """Human label for a duty-status code; falls back to the raw code."""
    if not code:
        return "Unknown"
    return DUTY_STATUS_LABELS.get(code, code)


# --------------------------------------------------------------------------- #
# Normalized snapshot model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HosTimers:
    """Hours-of-Service time REMAINING, in seconds."""

    drive_seconds: int
    shift_seconds: int
    break_seconds: int
    cycle_seconds: int

    @staticmethod
    def _hm(seconds: int) -> str:
        sign = "-" if seconds < 0 else ""
        s = abs(seconds)
        return f"{sign}{s // 3600}h {(s % 3600) // 60:02d}m"

    def drive_hm(self) -> str:
        return self._hm(self.drive_seconds)

    def shift_hm(self) -> str:
        return self._hm(self.shift_seconds)

    def break_hm(self) -> str:
        return self._hm(self.break_seconds)

    def cycle_hm(self) -> str:
        return self._hm(self.cycle_seconds)


@dataclass(frozen=True)
class ConnectionState:
    """Raw device/vehicle connection facts for the disconnect rule.

    has_vehicle == False means no vehicle is paired, so connection is UNKNOWN
    (we deliberately do not treat that as "disconnected").
    """

    has_vehicle: bool
    vehicle_status: str | None = None        # IN_MOTION / STATIONARY / OFFLINE
    vehicle_number: str | None = None
    last_telemetry: datetime | None = None   # timezone-aware UTC

    @property
    def is_offline(self) -> bool:
        return self.vehicle_status == VEHICLE_OFFLINE

    def staleness_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since last telemetry, or None if unknown."""
        if self.last_telemetry is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.last_telemetry).total_seconds()

    def is_stale(self, stale_minutes: int, now: datetime | None = None) -> bool:
        secs = self.staleness_seconds(now)
        return secs is not None and secs > stale_minutes * 60


@dataclass(frozen=True)
class DriverSnapshot:
    """A single driver's current state, normalized across providers."""

    driver_id: str
    name: str
    username: str | None
    duty_status_code: str | None
    hos: HosTimers
    connection: ConnectionState
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def duty_status_label(self) -> str:
        return label_for_status(self.duty_status_code)


@dataclass
class SnapshotResult:
    """Result of fetching snapshots for a set of configured drivers."""

    snapshots: list[DriverSnapshot] = field(default_factory=list)
    # Configured driver names that could not be matched to a roster entry.
    unresolved_names: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #
class ELDProvider(ABC):
    """Common interface every ELD provider implements."""

    #: short provider id, e.g. "factor" / "leader"
    name: str = "base"

    @abstractmethod
    def fetch_snapshots(self, drivers, all_active: bool = False) -> SnapshotResult:
        """Fetch normalized snapshots.

        If ``all_active`` is True, returns a snapshot for EVERY active driver on
        the roster (``drivers`` is ignored). Otherwise ``drivers`` is a list of
        config Driver objects (``.name`` + optional ``.eld_driver_id``) and each
        is resolved against the roster. Either way, HOS + connection data are
        joined and returned as a SnapshotResult.
        """
        raise NotImplementedError
