"""Factor ELD provider client — DriveHOS partner API.

Base URL: https://api.drivehos.app  (FACTOR_API_BASE_URL)
Auth: two static Partner API keys, sent together on every request —
    X-API-Provider-Key: <platform-wide key, FACTOR_API_KEY/LEADER_API_KEY>
    X-API-Company-Key:  <per-company key, Company.company_key>
Neither key expires, so there's no refresh/session lifecycle here.

Endpoints used:
    GET /v2/drivers                -> roster (driver_id, username, names)
    GET /v2/latest-driver-status   -> HOS seconds remaining + duty status code
    GET /v2/latest-vehicle-status  -> vehicle status/timestamp (connection facts)

This client fetches all three, resolves the configured drivers to provider
driver_ids (explicit id, else by full name, else by username), joins driver +
vehicle data on driver_id, and returns normalized DriverSnapshot objects.

Uses only the standard library (urllib) — no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("eld_alert_bot")

from .base import (
    ConnectionState,
    DriverSnapshot,
    ELDError,
    ELDProvider,
    HosTimers,
    SnapshotResult,
    Unauthorized,
    normalize_name,
)

ROSTER_PATH = "/v2/drivers"
DRIVER_STATUS_PATH = "/v2/latest-driver-status"
VEHICLE_STATUS_PATH = "/v2/latest-vehicle-status"

_PLACEHOLDER_IDS = {"", "replace_me"}


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp like '2026-06-03T03:13:03Z' to aware UTC."""
    if not value:
        return None
    try:
        # Python 3.11 handles the trailing 'Z', but normalize defensively.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FactorELD(ELDProvider):
    name = "factor"

    #: how many times to retry a 429 before giving up, and the base backoff.
    _MAX_RETRIES = 3
    _BACKOFF_BASE = 3.0

    def __init__(
        self,
        provider_key: str,
        company_key: str,
        base_url: str,
        timeout: float = 30.0,
        page_size: int = 200,
    ) -> None:
        self._provider_key = provider_key
        self._company_key = company_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._page_size = page_size

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Provider-Key": self._provider_key,
            "X-API-Company-Key": self._company_key,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET one page and return the parsed ResponseV2 envelope.

        Retries HTTP 429 (rate limit) with backoff, honouring a Retry-After
        header when present — one provider key is shared by every company plus
        the dashboard, so short bursts over the limit are expected. Other errors
        raise immediately.
        """
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")

        body: str | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = resp.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code == 401:
                    raise Unauthorized(f"{self.name}: HTTP 401 for {path} — {detail}") from exc
                if exc.code == 429 and attempt < self._MAX_RETRIES:
                    try:
                        wait = float(exc.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        wait = self._BACKOFF_BASE * (2 ** attempt)
                    wait = min(max(wait, 1.0), 60.0)
                    log.warning("%s: HTTP 429 for %s — retry %d/%d in %.0fs",
                                self.name, path, attempt + 1, self._MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ELDError(f"{self.name}: HTTP {exc.code} for {path} — {detail}") from exc
            except urllib.error.URLError as exc:
                raise ELDError(f"{self.name}: network error for {path} — {exc.reason}") from exc

        if body is None:  # exhausted retries on 429
            raise ELDError(f"{self.name}: HTTP 429 for {path} — rate limit, gave up "
                           f"after {self._MAX_RETRIES} retries")

        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ELDError(f"{self.name}: invalid JSON from {path}") from exc

        code = envelope.get("status_code")
        if code not in (None, 200):
            raise ELDError(
                f"{self.name}: {path} returned status_code={code} "
                f"— {envelope.get('description')}"
            )
        return envelope

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """GET every page of a list endpoint and return the concatenated data."""
        params = dict(params or {})
        params.setdefault("limit", self._page_size)
        page = 1
        rows: list[dict] = []
        while True:
            params["page"] = page
            env = self._get(path, params)
            data = env.get("data") or []
            if isinstance(data, list):
                rows.extend(data)
            else:  # single-object endpoint used as a list; stop
                if data:
                    rows.append(data)
                break
            total_pages = env.get("total_pages") or 1
            if page >= total_pages or not data:
                break
            page += 1
        return rows

    # ------------------------------------------------------------------ #
    # Raw fetches
    # ------------------------------------------------------------------ #
    def fetch_roster(self) -> list[dict]:
        return self._get_all(ROSTER_PATH)

    def fetch_driver_statuses(self) -> dict[str, dict]:
        return {r["driver_id"]: r for r in self._get_all(DRIVER_STATUS_PATH) if r.get("driver_id")}

    def fetch_vehicle_statuses(self) -> dict[str, dict]:
        """Vehicle rows keyed by driver_id; keeps the most recent per driver."""
        by_driver: dict[str, dict] = {}
        for r in self._get_all(VEHICLE_STATUS_PATH):
            did = r.get("driver_id")
            if not did:
                continue
            prev = by_driver.get(did)
            if prev is None:
                by_driver[did] = r
                continue
            # Keep the newer telemetry if duplicate driver rows appear.
            if (_parse_timestamp(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) > (
                _parse_timestamp(prev.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
            ):
                by_driver[did] = r
        return by_driver

    # ------------------------------------------------------------------ #
    # Resolution + snapshot assembly
    # ------------------------------------------------------------------ #
    @staticmethod
    def _roster_full_name(row: dict) -> str:
        return f"{(row.get('first_name') or '').strip()} {(row.get('last_name') or '').strip()}".strip()

    def _build_indexes(self, roster: list[dict]) -> tuple[dict, dict, dict]:
        by_id = {r["driver_id"]: r for r in roster if r.get("driver_id")}
        by_name: dict[str, dict] = {}
        by_username: dict[str, dict] = {}
        for r in roster:
            by_name.setdefault(normalize_name(self._roster_full_name(r)), r)
            if r.get("username"):
                by_username.setdefault(normalize_name(r["username"]), r)
        return by_id, by_name, by_username

    def _resolve(self, driver, by_id, by_name, by_username) -> dict | None:
        """Resolve a configured driver to a roster row, or None."""
        explicit = getattr(driver, "eld_driver_id", None)
        if explicit and normalize_name(explicit) not in _PLACEHOLDER_IDS:
            row = by_id.get(explicit)
            if row:
                return row
            # An explicit id that isn't in the roster is still usable as the id.
            return {"driver_id": explicit, "username": None, "first_name": "", "last_name": ""}
        return by_name.get(normalize_name(driver.name)) or by_username.get(normalize_name(driver.name))

    def _snapshot_from(self, driver_id, name, username, statuses, vehicles) -> DriverSnapshot:
        st = statuses.get(driver_id, {})
        veh = vehicles.get(driver_id)
        hos = HosTimers(
            drive_seconds=int(st.get("drive", 0) or 0),
            shift_seconds=int(st.get("shift", 0) or 0),
            break_seconds=int(st.get("break", 0) or 0),
            cycle_seconds=int(st.get("cycle", 0) or 0),
        )
        connection = ConnectionState(
            has_vehicle=veh is not None,
            vehicle_status=(veh or {}).get("status"),
            vehicle_number=(veh or {}).get("number"),
            last_telemetry=_parse_timestamp((veh or {}).get("timestamp")),
        )
        return DriverSnapshot(
            driver_id=driver_id,
            name=name,
            username=username or st.get("username"),
            duty_status_code=st.get("current_status"),
            hos=hos,
            connection=connection,
            raw={"status": st, "vehicle": veh},
        )

    def fetch_snapshots(self, drivers, all_active: bool = False) -> SnapshotResult:
        roster = self.fetch_roster()
        by_id, by_name, by_username = self._build_indexes(roster)
        statuses = self.fetch_driver_statuses()
        vehicles = self.fetch_vehicle_statuses()
        result = SnapshotResult()

        if all_active:
            # One snapshot per ACTIVE roster driver (config driver list ignored).
            for row in roster:
                if not row.get("active", True):
                    continue
                result.snapshots.append(
                    self._snapshot_from(
                        row["driver_id"],
                        self._roster_full_name(row) or row.get("username", ""),
                        row.get("username"),
                        statuses,
                        vehicles,
                    )
                )
            return result

        for driver in drivers:
            row = self._resolve(driver, by_id, by_name, by_username)
            if row is None:
                result.unresolved_names.append(driver.name)
                continue
            result.snapshots.append(
                self._snapshot_from(
                    row["driver_id"], driver.name, row.get("username"), statuses, vehicles
                )
            )
        return result
