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


def _as_int(value) -> int:
    """Coerce an API numeric field to int, tolerating strings/floats/nulls.

    HOS seconds have arrived as ints, but a bare ``int()`` on anything else the
    API might send ("3600", 3600.0, null) raises a TypeError/ValueError that is
    NOT an ELDError — it would escape the per-company error isolation in
    scheduler.run_cycle and abort the whole cycle. Unparseable => 0.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


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

    #: how many times to retry a transient failure before giving up, and the
    #: base backoff (doubled per attempt).
    _MAX_RETRIES = 3
    _BACKOFF_BASE = 3.0
    #: HTTP statuses worth retrying: 429 rate limit plus the transient gateway
    #: errors DriveHOS returns during a deploy. 500 is NOT here — a genuine
    #: server error repeats, and retrying it just delays the cycle.
    _RETRY_STATUSES = frozenset({429, 502, 503, 504})
    #: hard cap on pagination, so a bogus total_pages can't loop forever.
    _MAX_PAGES = 50

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

    def _retry_wait(self, headers, attempt: int) -> float:
        """Seconds to wait before the next attempt — Retry-After if the server
        sent a usable one, else exponential backoff. Clamped to 1..60s."""
        try:
            wait = float((headers or {}).get("Retry-After", ""))
        except (TypeError, ValueError):
            wait = self._BACKOFF_BASE * (2 ** attempt)
        return min(max(wait, 1.0), 60.0)

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET one page and return the parsed ResponseV2 envelope.

        Retries transient failures (429 rate limit — one provider key is shared
        by every company plus the dashboard, so short bursts over the limit are
        expected — plus 502/503/504 and network errors) with backoff, honouring
        a Retry-After header when present. Everything else raises immediately.
        """
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")

        body: str | None = None
        last_error = "no attempt made"
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
                last_error = f"HTTP {exc.code} — {detail}"
                if exc.code in self._RETRY_STATUSES and attempt < self._MAX_RETRIES:
                    wait = self._retry_wait(exc.headers, attempt)
                    log.warning("%s: HTTP %d for %s — retry %d/%d in %.0fs",
                                self.name, exc.code, path, attempt + 1,
                                self._MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ELDError(f"{self.name}: HTTP {exc.code} for {path} — {detail}") from exc
            except urllib.error.URLError as exc:
                # Timeouts / connection resets: transient by nature. Retrying
                # here keeps one blip from costing the whole company's cycle.
                last_error = f"network error — {exc.reason}"
                if attempt < self._MAX_RETRIES:
                    wait = self._retry_wait(None, attempt)
                    log.warning("%s: network error for %s (%s) — retry %d/%d in %.0fs",
                                self.name, path, exc.reason, attempt + 1,
                                self._MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ELDError(f"{self.name}: network error for {path} — {exc.reason}") from exc

        if body is None:  # defensive: every branch above either breaks or raises
            raise ELDError(f"{self.name}: {path} failed after {self._MAX_RETRIES} "
                           f"retries — {last_error}")

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
        """GET every page of a list endpoint and return the concatenated data.

        Only mapping rows are kept: every caller does ``row.get(...)``, and a
        stray scalar in ``data`` would raise an AttributeError that isn't an
        ELDError — i.e. it would escape per-company error isolation.
        """
        params = dict(params or {})
        params.setdefault("limit", self._page_size)
        page = 1
        rows: list[dict] = []
        while True:
            params["page"] = page
            env = self._get(path, params)
            data = env.get("data") or []
            if isinstance(data, dict):  # single-object endpoint used as a list
                rows.append(data)
                break
            if not isinstance(data, list):
                break
            rows.extend(r for r in data if isinstance(r, dict))
            total_pages = env.get("total_pages") or 1
            if page >= total_pages or not data:
                break
            if page >= self._MAX_PAGES:
                log.warning("%s: %s stopped at the %d-page cap (total_pages=%s)",
                            self.name, path, self._MAX_PAGES, total_pages)
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
            drive_seconds=_as_int(st.get("drive")),
            shift_seconds=_as_int(st.get("shift")),
            break_seconds=_as_int(st.get("break")),
            cycle_seconds=_as_int(st.get("cycle")),
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
                driver_id = row.get("driver_id")
                if not driver_id:
                    # Nothing to join HOS/vehicle data on, and no stable de-dup
                    # key for alert state — skip rather than crash the cycle.
                    log.warning("%s: roster row without driver_id skipped", self.name)
                    continue
                result.snapshots.append(
                    self._snapshot_from(
                        driver_id,
                        self._roster_full_name(row) or row.get("username", ""),
                        row.get("username"),
                        statuses,
                        vehicles,
                    )
                )
            return result

        for driver in drivers:
            row = self._resolve(driver, by_id, by_name, by_username)
            # A name/username match can land on a roster row that carries no
            # driver_id; treat that as unresolved, same as no match at all.
            if row is None or not row.get("driver_id"):
                result.unresolved_names.append(driver.name)
                continue
            result.snapshots.append(
                self._snapshot_from(
                    row["driver_id"], driver.name, row.get("username"), statuses, vehicles
                )
            )
        return result
