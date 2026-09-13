"""Encrypted per-company API keys, stored on the data volume.

Why this exists: a company's DriveHOS key used to live only in a host
environment variable named by `company_key_env`. That works for companies added
by editing config.yaml and redeploying, but not for one added through the admin
panel — a new host variable can't take effect without a restart, and the panel
has no way to create one.

So a key typed into the panel is written here instead, encrypted, under the same
`company_key_env` name the config already uses. `config.load_config()` looks up
the host environment first and falls back to this store, which keeps every
existing company reading its Railway variable exactly as before.

The file lives in DATA_DIR, so it must be on a mounted volume to survive a
redeploy — the same requirement the registry already has.

Encryption is Fernet (AES-128-CBC + HMAC) with the key derived from
DASHBOARD_SECRET_KEY. That protects the keys at rest on the volume and in any
volume backup; it is not protection against someone who already has both the
volume and the app's environment, which is the same trust boundary the host's
own environment variables sit behind.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

STORE_FILENAME = "company_keys.json"

# A company_key_env must be a usable environment-variable identifier: the loader
# looks it up with os.environ.get(), so a name with a space in it can never
# resolve (this is how the live "MILEMAX LLC" variable silently disabled that
# company every cycle).
_VALID_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class KeyStoreError(RuntimeError):
    pass


def is_valid_name(name: str) -> bool:
    return bool(name) and _VALID_NAME.fullmatch(name) is not None


def env_name_for(company_name: str) -> str:
    """Derive the conventional variable name for a company.

    "SOLEH EXPRESS INC" -> "SOLEH_EXPRESS_COMPANY_KEY", matching the names the
    existing 13 companies already use in config.yaml.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (company_name or "").upper()).strip("_")
    # Carrier suffixes carry no meaning in a variable name and only make it
    # longer; the existing names (WWH_COMPANY_KEY, ZOHA_COMPANY_KEY) drop them.
    slug = re.sub(r"_(INC|LLC|LTD|CORP|CO|COMPANY)$", "", slug)
    if not slug:
        raise KeyStoreError("company name has no usable characters for a variable name")
    if slug[0].isdigit():
        slug = f"C_{slug}"
    return f"{slug}_COMPANY_KEY"


def store_path(data_dir: str | None = None) -> Path:
    base = data_dir if data_dir is not None else os.environ.get("DATA_DIR", ".")
    return Path(base) / STORE_FILENAME


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise KeyStoreError(f"cryptography is required for the key store: {exc}") from exc
    secret = (os.environ.get("DASHBOARD_SECRET_KEY") or "").strip()
    if not secret:
        raise KeyStoreError(
            "DASHBOARD_SECRET_KEY must be set to read or write stored company keys"
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("company key store is unreadable (%s): %s", path, exc)
        return {}
    keys = data.get("keys")
    return keys if isinstance(keys, dict) else {}


def _write_raw(path: Path, keys: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"keys": keys}, indent=2), "utf-8")
    tmp.replace(path)


def get(env_name: str, data_dir: str | None = None) -> str | None:
    """Decrypt and return one stored key, or None if it isn't stored here.

    Never raises on a bad//missing store: config loading must keep working (and
    simply treat the company as unconfigured) rather than take the service down.
    """
    if not env_name:
        return None
    keys = _read_raw(store_path(data_dir))
    token = keys.get(env_name)
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        log.error(
            "stored key for %s could not be decrypted (%s) — has "
            "DASHBOARD_SECRET_KEY changed since it was saved?", env_name, exc,
        )
        return None


def put(env_name: str, value: str, data_dir: str | None = None) -> None:
    if not is_valid_name(env_name):
        raise KeyStoreError(
            f"{env_name!r} is not a valid variable name — letters, digits and "
            f"underscores only, and it may not start with a digit"
        )
    value = (value or "").strip()
    if not value:
        raise KeyStoreError("API key is empty")
    path = store_path(data_dir)
    keys = _read_raw(path)
    keys[env_name] = _fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    _write_raw(path, keys)
    log.info("stored company API key under %s", env_name)


def delete(env_name: str, data_dir: str | None = None) -> bool:
    path = store_path(data_dir)
    keys = _read_raw(path)
    if env_name not in keys:
        return False
    del keys[env_name]
    _write_raw(path, keys)
    return True


def names(data_dir: str | None = None) -> list[str]:
    """Variable names held here — for showing which keys the panel manages."""
    return sorted(_read_raw(store_path(data_dir)))
