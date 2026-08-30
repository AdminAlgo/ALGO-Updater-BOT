"""Configuration loading and validation.

Loads secrets from .env (via python-dotenv) and non-secret settings from
config.yaml (via PyYAML), validates that everything required is present and
well-formed, and exposes a single immutable ``Config`` object (built from
dataclasses) that the rest of the application imports.

Public API:
    load_config(...) -> Config        # load + validate, raises ConfigError
    Config, Company, Driver, Secrets  # dataclasses
    ConfigError                       # raised with a list of every problem found
    mask_chat_id(value) -> str        # "-1001234567890" -> "-100****7890"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "PyYAML is required. Install dependencies with: pip install -r requirements.txt"
    ) from exc

try:
    from dotenv import load_dotenv
except ImportError as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "python-dotenv is required. Install dependencies with: pip install -r requirements.txt"
    ) from exc


# Required environment variables (secrets), unconditionally. All must be
# non-empty. Per-platform PROVIDER keys (FACTOR_API_KEY / LEADER_API_KEY) are
# validated separately, conditionally on whether an enabled company actually
# uses that provider (see load_config) — same isolation principle as
# per-company keys below: a platform nobody uses shouldn't block startup.
REQUIRED_ENV_VARS = (
    "FACTOR_API_BASE_URL",
    "LEADER_API_BASE_URL",
    "TELEGRAM_BOT_TOKEN",
)

# Provider (platform-wide) API key env var per provider — sent as the
# X-API-Provider-Key header alongside a company's X-API-Company-Key.
PROVIDER_KEY_ENV_VARS = {"factor": "FACTOR_API_KEY", "leader": "LEADER_API_KEY"}

VALID_PROVIDERS = ("factor", "leader")


class ConfigError(Exception):
    """Raised when configuration is missing or malformed.

    The message lists every problem found, one per line, so the user can fix
    them all in one pass instead of one at a time.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"Configuration is invalid ({len(problems)} problem(s) found):\n{body}"
        )


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Secrets:
    """Secret values loaded from .env.

    Provider auth is a static per-platform Partner API key
    (``X-API-Provider-Key``), paired with each company's own key
    (``Company.company_key`` / ``X-API-Company-Key``). Neither expires, so
    there's no refresh/session lifecycle to manage.
    """

    factor_api_base_url: str
    leader_api_base_url: str
    telegram_bot_token: str
    factor_api_key: str | None = field(default=None, repr=False)
    leader_api_key: str | None = field(default=None, repr=False)

    def base_url_for(self, provider: str) -> str:
        return self.factor_api_base_url if provider == "factor" else self.leader_api_base_url

    def provider_key_for(self, provider: str) -> str | None:
        return self.factor_api_key if provider == "factor" else self.leader_api_key


@dataclass(frozen=True)
class Driver:
    name: str
    # Optional: when omitted (or "REPLACE_ME"), the provider resolves the id
    # from the roster by matching this name / username.
    eld_driver_id: str | None = None


@dataclass(frozen=True)
class Company:
    name: str
    provider: str  # "factor" or "leader" — selects which platform's provider key to use
    driver_group_chat_id: str
    monitor_all_drivers: bool = False  # if True, watch every active roster driver
    # Set false to pause this company entirely (no polling, no alerts) while
    # keeping its config for later. A disabled company also doesn't need a
    # working company_key_env — see load_config's isolation check.
    enabled: bool = True
    # Optional per-company central chat (30-min + violations). Falls back to the
    # global team_group_chat_id when not set — lets each company have its own.
    team_group_chat_id: str | None = None
    # Optional per-company closing line for the low-hours message (replaces the
    # default "Please let us know if you will need more.").
    low_hours_closing: str | None = None
    drivers: list[Driver] = field(default_factory=list)
    # Name of the .env variable holding this company's X-API-Company-Key.
    company_key_env: str | None = None
    # Resolved value of that variable (never written to config.yaml or logs).
    company_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LowHoursThresholds:
    driver_group: list[int]
    team_group: list[int]


@dataclass(frozen=True)
class Config:
    """Fully validated application configuration."""

    poll_interval_seconds: int
    team_group_chat_id: str
    low_hours_thresholds_minutes: LowHoursThresholds
    disconnect_realert_minutes: int
    disconnect_stale_minutes: int
    shift_limit_hours: int
    connection_required_statuses: list[str]
    companies: list[Company]
    secrets: Secrets
    # Optional: a URL template for a driver's HOS log, appended to each alert.
    # Supports {driver_id}, {username}, {name}. None/empty = no link added.
    driver_log_url_template: str | None = None
    # If True, attach a generated HOS "ring card" image to each alert.
    attach_log_image: bool = False
    # If False, ELD-disconnect alerts are not sent (only low-hours + violations).
    disconnect_alerts_enabled: bool = True
    # Welfare check: alert when a driver stays On Duty (continuously) at least
    # this many hours. 0 disables it.
    on_duty_alert_hours: float = 2
    # Manual driver -> Telegram @handle overrides (keyed by driver name), used
    # when auto-capture from the group can't identify the driver.
    manual_driver_tags: dict = field(default_factory=dict)
    # Telegram user IDs allowed to change group assignments via bot commands.
    # Empty preserves the original command behavior during initial setup.
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def driver_count(self) -> int:
        return sum(len(c.drivers) for c in self.companies)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def mask_chat_id(value: Any) -> str:
    """Mask the middle of a chat ID for safe display.

    Keeps the first 4 and last 4 characters, replacing the middle with "****".
    Example: "-1001234567890" -> "-100****7890". Short values are fully masked.
    """
    s = str(value)
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}****{s[-4:]}"


def mask_secret(value: Any) -> str:
    """Mask a secret for display, revealing only the last 4 characters."""
    s = str(value)
    if len(s) <= 4:
        return "****"
    return f"****{s[-4:]}"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_list_problems(value: Any, label: str) -> list[str]:
    """Validate that ``value`` is a list of positive ints (empty = no alerts)."""
    problems: list[str] = []
    if value is None:
        return problems  # missing => treated as empty (no thresholds)
    if not isinstance(value, list):
        problems.append(f"{label} must be a list of minutes (or empty for none)")
        return problems
    for i, item in enumerate(value):
        if not _is_int(item) or item <= 0:
            problems.append(f"{label}[{i}] must be a positive integer, got {item!r}")
    return problems


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _load_secrets(problems: list[str]) -> Secrets | None:
    """Read required env vars, appending a problem for each missing/empty one."""
    values: dict[str, str] = {}
    for name in REQUIRED_ENV_VARS:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            problems.append(f"missing or empty environment variable: {name}")
        else:
            values[name] = raw.strip()

    if len(values) != len(REQUIRED_ENV_VARS):
        return None

    def _opt(name: str) -> str | None:
        raw = os.environ.get(name)
        return raw.strip() if raw and raw.strip() else None

    return Secrets(
        factor_api_base_url=values["FACTOR_API_BASE_URL"],
        leader_api_base_url=values["LEADER_API_BASE_URL"],
        telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
        factor_api_key=_opt("FACTOR_API_KEY"),
        leader_api_key=_opt("LEADER_API_KEY"),
    )


def _load_admin_user_ids(problems: list[str]) -> frozenset[int]:
    """Parse optional comma-separated Telegram administrator user IDs."""
    raw = (os.environ.get("ADMIN_TELEGRAM_USER_IDS") or "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        try:
            user_id = int(value)
        except ValueError:
            problems.append(
                "ADMIN_TELEGRAM_USER_IDS must contain comma-separated numeric user IDs"
            )
            continue
        if user_id <= 0:
            problems.append("ADMIN_TELEGRAM_USER_IDS values must be positive")
            continue
        ids.add(user_id)
    return frozenset(ids)


def _load_yaml(path: Path, problems: list[str]) -> dict[str, Any] | None:
    if not path.exists():
        problems.append(f"config file not found: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        problems.append(f"config file is not valid YAML: {exc}")
        return None
    if not isinstance(data, dict):
        problems.append(f"config file must contain a YAML mapping at the top level: {path}")
        return None
    return data


def _parse_companies(raw_companies: Any, problems: list[str]) -> list[Company]:
    companies: list[Company] = []
    if not isinstance(raw_companies, list) or not raw_companies:
        problems.append("'companies' must be a non-empty list")
        return companies

    for ci, raw in enumerate(raw_companies):
        where = f"companies[{ci}]"
        if not isinstance(raw, dict):
            problems.append(f"{where} must be a mapping")
            continue

        name = raw.get("name")
        provider = raw.get("provider")
        chat_id = raw.get("driver_group_chat_id")
        monitor_all = bool(raw.get("monitor_all_drivers", False))
        enabled = bool(raw.get("enabled", True))
        raw_drivers = raw.get("drivers")

        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}.name is required and must be a non-empty string")
        if provider not in VALID_PROVIDERS:
            problems.append(
                f"{where}.provider must be one of {VALID_PROVIDERS}, got {provider!r}"
            )
        if not isinstance(chat_id, str) or not chat_id.strip():
            problems.append(
                f"{where}.driver_group_chat_id is required and must be a non-empty string"
            )

        # Company API key — only required for an ENABLED company (a disabled
        # company doesn't need to authenticate at all; see the reliability
        # note in CLAUDE_HANDOFF.md).
        company_key_env = raw.get("company_key_env")
        company_key: str | None = None
        if isinstance(company_key_env, str) and company_key_env.strip():
            company_key_env = company_key_env.strip()
            raw_key = os.environ.get(company_key_env)
            company_key = raw_key.strip() if raw_key and raw_key.strip() else None
            if enabled and company_key is None:
                problems.append(
                    f"{where}: environment variable {company_key_env} "
                    f"(company_key_env) is missing or empty"
                )
        elif enabled:
            problems.append(f"{where}.company_key_env is required for an enabled company")
        else:
            company_key_env = None

        drivers: list[Driver] = []
        if monitor_all:
            # Roster-wide monitoring: an explicit driver list is optional/ignored.
            pass
        elif not isinstance(raw_drivers, list) or not raw_drivers:
            problems.append(
                f"{where}.drivers must be a non-empty list "
                f"(or set monitor_all_drivers: true)"
            )
        elif isinstance(raw_drivers, list):
            for di, rd in enumerate(raw_drivers):
                dwhere = f"{where}.drivers[{di}]"
                if not isinstance(rd, dict):
                    problems.append(f"{dwhere} must be a mapping")
                    continue
                dname = rd.get("name")
                eld_id = rd.get("eld_driver_id")  # optional; resolved from roster
                if not isinstance(dname, str) or not dname.strip():
                    problems.append(f"{dwhere}.name is required and must be a non-empty string")
                    continue
                resolved_id = (
                    eld_id.strip()
                    if isinstance(eld_id, str) and eld_id.strip() and eld_id.strip() != "REPLACE_ME"
                    else None
                )
                drivers.append(Driver(name=dname.strip(), eld_driver_id=resolved_id))

        if (
            isinstance(name, str)
            and provider in VALID_PROVIDERS
            and isinstance(chat_id, str)
        ):
            companies.append(
                Company(
                    name=name.strip(),
                    provider=provider,
                    driver_group_chat_id=chat_id.strip(),
                    monitor_all_drivers=monitor_all,
                    enabled=enabled,
                    team_group_chat_id=(raw.get("team_group_chat_id") or "").strip() or None,
                    low_hours_closing=(raw.get("low_hours_closing") or "").strip() or None,
                    drivers=drivers,
                    company_key_env=company_key_env,
                    company_key=company_key,
                )
            )

    return companies


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path = ".env",
) -> Config:
    """Load and validate configuration from .env and config.yaml.

    Raises:
        ConfigError: if anything required is missing or malformed. The error
            message lists every problem found.
    """
    problems: list[str] = []

    # Load .env (does not override already-set process env vars).
    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(env_file, override=False)
    # If .env is absent, env vars may still be set in the real environment;
    # _load_secrets reports anything still missing.

    secrets = _load_secrets(problems)
    admin_user_ids = _load_admin_user_ids(problems)

    data = _load_yaml(Path(config_path), problems)
    if data is None:
        # Cannot proceed without a YAML mapping; surface everything so far.
        raise ConfigError(problems)

    # Scalars
    poll = data.get("poll_interval_seconds")
    if not _is_int(poll) or poll <= 0:
        problems.append("poll_interval_seconds must be a positive integer")

    team_chat = data.get("team_group_chat_id")
    if not isinstance(team_chat, str) or not team_chat.strip():
        problems.append("team_group_chat_id is required and must be a non-empty string")

    realert = data.get("disconnect_realert_minutes")
    if not _is_int(realert) or realert <= 0:
        problems.append("disconnect_realert_minutes must be a positive integer")

    stale = data.get("disconnect_stale_minutes")
    if not _is_int(stale) or stale <= 0:
        problems.append("disconnect_stale_minutes must be a positive integer")

    shift_limit = data.get("shift_limit_hours")
    if not _is_int(shift_limit) or shift_limit <= 0:
        problems.append("shift_limit_hours must be a positive integer")

    statuses = data.get("connection_required_statuses")
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(isinstance(s, str) and s.strip() for s in statuses)
    ):
        problems.append(
            "connection_required_statuses must be a non-empty list of strings"
        )

    # Thresholds
    raw_thresholds = data.get("low_hours_thresholds_minutes")
    driver_thr: list[int] = []
    team_thr: list[int] = []
    if not isinstance(raw_thresholds, dict):
        problems.append("low_hours_thresholds_minutes must be a mapping")
    else:
        driver_thr = raw_thresholds.get("driver_group")  # type: ignore[assignment]
        team_thr = raw_thresholds.get("team_group")  # type: ignore[assignment]
        problems.extend(
            _int_list_problems(driver_thr, "low_hours_thresholds_minutes.driver_group")
        )
        problems.extend(
            _int_list_problems(team_thr, "low_hours_thresholds_minutes.team_group")
        )

    companies = _parse_companies(data.get("companies"), problems)

    # Provider (platform-wide) key — only required if an ENABLED company
    # actually uses that provider (same isolation principle as company keys).
    if secrets is not None:
        providers_in_use = {c.provider for c in companies if c.enabled}
        for provider, env_name in PROVIDER_KEY_ENV_VARS.items():
            if provider in providers_in_use and not secrets.provider_key_for(provider):
                problems.append(
                    f"missing or empty environment variable: {env_name} "
                    f"(required because an enabled company uses provider {provider!r})"
                )

    # Optional driver-log URL template.
    log_tpl = data.get("driver_log_url_template")
    if log_tpl is not None and not isinstance(log_tpl, str):
        problems.append("driver_log_url_template must be a string if provided")
        log_tpl = None
    elif isinstance(log_tpl, str):
        log_tpl = log_tpl.strip() or None

    if problems:
        raise ConfigError(problems)

    return Config(
        poll_interval_seconds=poll,
        team_group_chat_id=team_chat.strip(),
        low_hours_thresholds_minutes=LowHoursThresholds(
            driver_group=list(driver_thr or []),
            team_group=list(team_thr or []),
        ),
        disconnect_realert_minutes=realert,
        disconnect_stale_minutes=stale,
        shift_limit_hours=shift_limit,
        connection_required_statuses=[s.strip() for s in statuses],
        companies=companies,
        secrets=secrets,  # type: ignore[arg-type]  # guaranteed non-None: no problems
        driver_log_url_template=log_tpl,
        attach_log_image=bool(data.get("attach_log_image", False)),
        disconnect_alerts_enabled=bool(data.get("disconnect_alerts_enabled", True)),
        on_duty_alert_hours=float(data.get("on_duty_alert_hours", 2) or 0),
        manual_driver_tags=(data.get("manual_driver_tags") or {}) if isinstance(
            data.get("manual_driver_tags") or {}, dict) else {},
        admin_user_ids=admin_user_ids,
    )
