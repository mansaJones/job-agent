"""Client configuration — read from apply_client/.env (or real environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
ENV_PATH = PACKAGE_DIR / ".env"


class ConfigError(Exception):
    """Missing or invalid client configuration."""


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Local"


@dataclass(frozen=True)
class ClientConfig:
    jetson_url: str
    apply_client_token: str
    chrome_profile_dir: Path
    download_dir: Path
    poll_interval_seconds: float = 20.0
    human_timeout_minutes: float = 25.0


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {raw!r}")
    return value


def load_config(env_path: Path = ENV_PATH, require_jetson: bool = True) -> ClientConfig:
    """Load config from .env. Fails fast with a clear message when required values are missing.

    Args:
        require_jetson: False for --dry-run, which never talks to the Jetson.
    """
    load_dotenv(env_path, override=False)

    jetson_url = os.environ.get("JETSON_URL", "").strip().rstrip("/")
    token = os.environ.get("APPLY_CLIENT_TOKEN", "").strip()

    if require_jetson:
        missing = [n for n, v in (("JETSON_URL", jetson_url), ("APPLY_CLIENT_TOKEN", token)) if not v]
        if missing:
            raise ConfigError(
                f"Missing {', '.join(missing)} — copy apply_client/.env.example to "
                f"{env_path} and fill it in. APPLY_CLIENT_TOKEN must match the Jetson's "
                "config/secrets.env."
            )
        if not jetson_url.startswith(("http://", "https://")):
            raise ConfigError(f"JETSON_URL must start with http:// or https://, got {jetson_url!r}")

    profile = os.environ.get("CHROME_PROFILE_DIR", "").strip()
    downloads = os.environ.get("DOWNLOAD_DIR", "").strip()
    return ClientConfig(
        jetson_url=jetson_url,
        apply_client_token=token,
        chrome_profile_dir=Path(profile) if profile else _local_appdata() / "job-agent" / "chrome-profile",
        download_dir=Path(downloads) if downloads else _local_appdata() / "job-agent" / "downloads",
        poll_interval_seconds=_number("POLL_INTERVAL_SECONDS", 20.0),
        human_timeout_minutes=_number("HUMAN_TIMEOUT_MINUTES", 25.0),
    )
