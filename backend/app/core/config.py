"""Application settings, loaded exclusively from the environment.

Secrets never carry a default value: an unset key stays ``None`` and the
feature that needs it fails loudly instead of silently using a placeholder.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/offers"


class Settings(BaseSettings):
    """Typed view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Core ──────────────────────────────────────────────────────────
    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    database_url: str = DEV_DATABASE_URL
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # ── LLM ───────────────────────────────────────────────────────────
    anthropic_api_key: SecretStr | None = None
    #: Hot path: vacancy re-rank, Telegram post parsing. Runs thousands of times.
    anthropic_model: str = "claude-sonnet-5"
    #: Cold path: resume extraction, cover letters. Runs rarely, quality matters.
    anthropic_model_heavy: str = "claude-opus-5"
    llm_rerank_top_n: Annotated[int, Field(ge=1, le=200)] = 30
    llm_max_cost_per_run_usd: Annotated[float, Field(ge=0)] = 2.0

    # ── Embeddings ────────────────────────────────────────────────────
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: Annotated[int, Field(ge=1)] = 1024

    # ── HTTP ──────────────────────────────────────────────────────────
    http_timeout: Annotated[float, Field(gt=0)] = 30.0
    user_agent: str = "who-wants-an-offer/1.0 (+https://github.com/nurzhan2/Who_wants_an_offer-)"
    #: Set in dev to cache source responses on disk and stop hammering APIs.
    http_cache_dir: Path | None = None

    # ── Sources: optional API keys ────────────────────────────────────
    adzuna_app_id: str | None = None
    adzuna_app_key: SecretStr | None = None
    jooble_api_key: SecretStr | None = None
    rapidapi_key: SecretStr | None = None
    themuse_api_key: SecretStr | None = None
    findwork_token: SecretStr | None = None

    # ── Telegram source (Telethon user session) ───────────────────────
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_session_path: Path | None = None

    # ── Notifications (Telegram bot) ──────────────────────────────────
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    match_score_alert_threshold: Annotated[int, Field(ge=0, le=100)] = 80
    quiet_hours: str | None = None

    # ── Pipeline ──────────────────────────────────────────────────────
    full_run_interval_hours: Annotated[int, Field(ge=1)] = 12
    incremental_run_interval_hours: Annotated[int, Field(ge=1)] = 3
    max_vacancy_age_days: Annotated[int, Field(ge=1)] = 45

    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, value: Any) -> Any:
        """Treat ``KEY=`` in a .env file as "not configured", not as an empty value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        """Accept a comma-separated list, which is what an env var realistically holds."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _production_requires_explicit_config(self) -> "Settings":
        """Refuse to boot production on development fallbacks."""
        if self.environment != "production":
            return self
        missing: list[str] = []
        if self.database_url == DEV_DATABASE_URL:
            missing.append("DATABASE_URL")
        if self.anthropic_api_key is None:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise ValueError(f"ENVIRONMENT=production requires: {', '.join(missing)}")
        return self

    @property
    def is_production(self) -> bool:
        """True when the app runs with production logging and error verbosity."""
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
