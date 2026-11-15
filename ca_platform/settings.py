"""Paths, configuration loading and secret resolution."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
PROMPTS = ROOT / "prompts"
WEB = ROOT / "web"
DATA = Path(os.environ.get("CA_DATA_DIR", ROOT / "data"))
RAW = DATA / "raw"
REPORTS = DATA / "reports"
INBOX_ET = DATA / "inbox" / "et"
MANUAL = DATA / "manual"
SECRETS_DIR = DATA / "secrets"

IST = timezone(timedelta(hours=5, minutes=30))

load_dotenv(ROOT / ".env")

for _d in (DATA, RAW, REPORTS, INBOX_ET, MANUAL, SECRETS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def now_ist() -> datetime:
    return datetime.now(IST)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_ist()).isoformat(timespec="seconds")


@lru_cache(maxsize=None)
def _load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG / f"{name}.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def config(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml (cached; call reload_config() after edits)."""
    return _load_yaml(name)


def reload_config() -> None:
    _load_yaml.cache_clear()


def config_versions() -> dict[str, str]:
    return {n: str(config(n).get("version", "unversioned")) for n in
            ("models", "rules", "user", "horizons", "tax_rates", "sources")}


KEYRING_SERVICE = "personal-ca-platform"


def get_secret(name: str) -> str | None:
    """OS keychain first (spec §16), then environment / .env. Never logged."""
    try:
        import keyring
        val = keyring.get_password(KEYRING_SERVICE, name)
        if val:
            return val
    except Exception:
        pass
    val = os.environ.get(name)
    return val or None


def set_secret(name: str, value: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, name, value)


def canonical_hash(obj: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def user_agent() -> str:
    return os.environ.get("CA_HTTP_USER_AGENT", "PersonalCA/0.2 (personal research)")
