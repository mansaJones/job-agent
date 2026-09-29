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
RESUMES_DIR: Path = PROJECT_ROOT / "resumes"
GENERATED_RESUMES_DIR: Path = DATA_DIR / "generated_resumes"


# ---------------------------------------------------------------------------
# Profile models — your job preferences
# ---------------------------------------------------------------------------

class SkillsConfig(BaseModel):
    must_have: list[str] = Field(default_factory=list)
    # Gate skills — a job must list at least ONE of these to score well
    must_have_any: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)


class SearchLaneConfig(BaseModel):
    """One role family to search for (e.g. frontend dev vs. marketing manager)."""

    name: str
    enabled: bool = True
    target_roles: list[str] = Field(default_factory=list)
    target_field: str = ""
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    resume_version: str = ""


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


class ContactConfig(BaseModel):
    """Contact details for resumes — not all of these are in the LinkedIn export."""

    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""


class MaintenanceConfig(BaseModel):
    """Housekeeping settings — stale listing purge, etc."""

    stale_listing_max_age_days: int = 30
    preserve_statuses: list[str] = Field(default_factory=lambda: ["approved", "applied"])


class ProfileConfig(BaseModel):
    """Your target job profile — loaded from config/profile.yaml."""

    search_lanes: dict[str, SearchLaneConfig] = Field(default_factory=dict)
    preferences: PreferencesConfig = Field(default_factory=PreferencesConfig)
    blacklist: BlacklistConfig = Field(default_factory=BlacklistConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    contact: ContactConfig = Field(default_factory=ContactConfig)

    @property
    def enabled_lanes(self) -> list[SearchLaneConfig]:
        """Lanes with enabled=True, in config order."""
        return [lane for lane in self.search_lanes.values() if lane.enabled]

    def get_lane(self, name: str) -> SearchLaneConfig | None:
        """Look up a lane by name (enabled or not)."""
        return self.search_lanes.get(name)


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
    for name, lane_data in (raw.get("search_lanes") or {}).items():
        lane_data["name"] = name
    config = ProfileConfig(**raw)
    enabled = [lane.name for lane in config.enabled_lanes]
    logger.info("Loaded profile: %d search lanes (%d enabled: %s)",
                len(config.search_lanes), len(enabled), ", ".join(enabled) or "none")
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
      - search_queries are NOT inherited — a board with none gets its queries
        built per lane at scrape time from each lane's target_roles. A board
        with explicit search_queries runs them once per enabled lane.
      - If a board has no location → use profile.preferences.location
      - If a board has no radius_miles (None) → use profile.preferences.max_commute_miles
    """
    for board in boards.boards.values():
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
    for directory in [DATA_DIR, LOGS_DIR, HTML_SNAPSHOTS_DIR, DATA_DIR / "backups",
                      GENERATED_RESUMES_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

    return AppSettings(
        profile=profile,
        boards=boards,
        secrets=secrets,
    )
