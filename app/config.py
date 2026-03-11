"""Configuration system — loads YAML profiles, board configs, and env secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolve project root — walks up from this file to find `config/`
# Falls back to /home/agent if nothing found (production Jetson path)
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """Walk upward from this file to locate the project root (contains config/)."""
    current = Path(__file__).resolve().parent
    for _ in range(5):
        if (current / "config").is_dir():
            return current
        current = current.parent
    # Fallback for production Jetson layout
    return Path("/home/agent")


PROJECT_ROOT: Path = _find_project_root()
CONFIG_DIR: Path = PROJECT_ROOT / "config"
DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
HTML_SNAPSHOTS_DIR: Path = DATA_DIR / "html_snapshots"


# ---------------------------------------------------------------------------
# Profile models — your job preferences
# ---------------------------------------------------------------------------

class SkillsConfig(BaseModel):
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)


class PreferencesConfig(BaseModel):
    location: str = ""
    remote_ok: bool = True
    hybrid_ok: bool = True
    onsite_ok: bool = False
    max_commute_miles: int = 40
    salary_min: int = 0
    experience_years: int = 0


class BlacklistConfig(BaseModel):
    companies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("companies", "keywords", mode="before")
    @classmethod
    def lowercase_entries(cls, v: list[str]) -> list[str]:
        return [entry.lower().strip() for entry in v] if v else []


class ProfileConfig(BaseModel):
    """Your target job profile — loaded from config/profile.yaml."""

    target_roles: list[str] = Field(default_factory=list)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    preferences: PreferencesConfig = Field(default_factory=PreferencesConfig)
    blacklist: BlacklistConfig = Field(default_factory=BlacklistConfig)


# ---------------------------------------------------------------------------
# Board-level scraper config
# ---------------------------------------------------------------------------

class BoardConfig(BaseModel):
    """Per-board scraper configuration — loaded from config/boards.yaml."""

    name: str
    enabled: bool = True
    module: str  # e.g. "app.scrapers.indeed"
    base_url: str = ""
    search_queries: list[str] = Field(default_factory=list)
    location: str = ""
    radius_miles: int | None = None  # None = inherit from profile; 0 = no radius filter
    max_pages: int = 3
    delay_min: float = 10.0
    delay_max: float = 30.0
    headers: dict[str, str] = Field(default_factory=dict)
    # Blacklist scope: "all" checks title+description (default),
    # "title_only" checks only the title (useful for API scrapers
    # where descriptions contain verbose boilerplate).
    blacklist_scope: str = "all"


class BoardsConfig(BaseModel):
    """All board configs keyed by board name."""

    boards: dict[str, BoardConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Secrets / environment config
# ---------------------------------------------------------------------------

class SecretsConfig(BaseSettings):
    """Loaded from config/secrets.env (or real environment variables)."""

    model_config = SettingsConfigDict(
        env_file=str(CONFIG_DIR / "secrets.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b-instruct-q4_K_M"


# ---------------------------------------------------------------------------
# App-wide settings aggregate
# ---------------------------------------------------------------------------

class AppSettings(BaseModel):
    """Top-level container holding all config objects."""

    profile: ProfileConfig
    boards: BoardsConfig
    secrets: SecretsConfig
    db_path: Path = DATA_DIR / "jobs.db"
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return the parsed dict. Returns empty dict on failure."""
    if not path.exists():
        logger.warning("Config file not found: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_profile(path: Path | None = None) -> ProfileConfig:
    """Load job profile from YAML."""
    path = path or CONFIG_DIR / "profile.yaml"
    raw = _load_yaml(path)
    config = ProfileConfig(**raw)
    logger.info("Loaded profile: %d target roles, %d must-have skills",
                len(config.target_roles), len(config.skills.must_have))
    return config


def load_boards(path: Path | None = None) -> BoardsConfig:
    """Load board scraper configs from YAML."""
    path = path or CONFIG_DIR / "boards.yaml"
    raw = _load_yaml(path)
    boards: dict[str, BoardConfig] = {}
    for name, board_data in raw.get("boards", {}).items():
        board_data["name"] = name
        boards[name] = BoardConfig(**board_data)
    config = BoardsConfig(boards=boards)
    enabled = [b.name for b in config.boards.values() if b.enabled]
    logger.info("Loaded %d board configs (%d enabled)", len(config.boards), len(enabled))
    return config


def load_secrets() -> SecretsConfig:
    """Load secrets from env file / environment."""
    return SecretsConfig()


def _resolve_board_defaults(boards: BoardsConfig, profile: ProfileConfig) -> None:
    """Fill in board-level gaps from profile — profile.yaml is the single source of truth.

    Rules:
      - If a board has no search_queries → generate from profile.target_roles (lowercased)
      - If a board has no location → use profile.preferences.location
      - If a board has no radius_miles (0) → use profile.preferences.max_commute_miles
    """
    for board in boards.boards.values():
        if not board.search_queries:
            board.search_queries = [r.lower() for r in profile.target_roles]
            logger.debug("Board '%s': inherited %d search queries from profile",
                         board.name, len(board.search_queries))

        if not board.location:
            board.location = profile.preferences.location
            logger.debug("Board '%s': inherited location '%s' from profile",
                         board.name, board.location)

        if board.radius_miles is None:
            board.radius_miles = profile.preferences.max_commute_miles
            logger.debug("Board '%s': inherited radius %d mi from profile",
                         board.name, board.radius_miles)


def load_settings() -> AppSettings:
    """Load the full application settings from all config sources."""
    profile = load_profile()
    boards = load_boards()
    secrets = load_secrets()

    # Resolve shared defaults — profile is the single source of truth
    _resolve_board_defaults(boards, profile)

    # Ensure critical directories exist
    for directory in [DATA_DIR, LOGS_DIR, HTML_SNAPSHOTS_DIR, DATA_DIR / "backups"]:
        directory.mkdir(parents=True, exist_ok=True)

    return AppSettings(
        profile=profile,
        boards=boards,
        secrets=secrets,
    )
