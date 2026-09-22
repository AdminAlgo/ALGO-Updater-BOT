"""config.yaml read/mutate/atomic-write helpers for the dashboard.

Every write validates by writing to a temp file and calling load_config()
against it first — only replaces the real config.yaml if that succeeds, so a
bad edit from the dashboard can't brick the scheduler.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.config import ConfigError, load_config


def load_raw(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text("utf-8")) or {}


def validate_and_write(data: dict, config_path: str, env_path: str) -> None:
    path = Path(config_path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    try:
        load_config(str(tmp), env_path)
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


def set_company_enabled(company_name: str, enabled: bool, config_path: str, env_path: str) -> None:
    data = load_raw(config_path)
    for company in data.get("companies", []):
        if company.get("name") == company_name:
            company["enabled"] = enabled
            break
    else:
        raise ValueError(f"company not found: {company_name}")
    validate_and_write(data, config_path, env_path)


def add_company(company: dict, config_path: str, env_path: str) -> None:
    data = load_raw(config_path)
    data.setdefault("companies", []).append(company)
    validate_and_write(data, config_path, env_path)


def update_company(company_name: str, updates: dict, config_path: str, env_path: str) -> None:
    """Patch a company's editable display fields (name, USDOT, MC#, closing line).

    Does not touch provider/company_key_env/enabled — those have their own
    dedicated flows (set_company_enabled, the add form, Railway/.env directly).
    """
    data = load_raw(config_path)
    for company in data.get("companies", []):
        if company.get("name") == company_name:
            company.update({k: v for k, v in updates.items() if v is not None})
            break
    else:
        raise ValueError(f"company not found: {company_name}")
    validate_and_write(data, config_path, env_path)


def set_company_fields(company_name: str, fields: dict, config_path: str, env_path: str) -> None:
    """Set company keys; a value of None or [] REMOVES the key (so clearing a
    language or re-enabling every alert type leaves config.yaml tidy)."""
    data = load_raw(config_path)
    for company in data.get("companies", []):
        if company.get("name") == company_name:
            for key, value in fields.items():
                if value is None or value == []:
                    company.pop(key, None)
                else:
                    company[key] = value
            break
    else:
        raise ValueError(f"company not found: {company_name}")
    validate_and_write(data, config_path, env_path)


def set_settings(fields: dict, config_path: str, env_path: str) -> None:
    """Patch top-level settings (the Settings page). Validated like every write."""
    data = load_raw(config_path)
    for key, value in fields.items():
        if key == "low_hours_thresholds_minutes.driver_group":
            data.setdefault("low_hours_thresholds_minutes", {})["driver_group"] = value
        else:
            data[key] = value
    validate_and_write(data, config_path, env_path)
