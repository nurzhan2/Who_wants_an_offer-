"""Application settings, loaded exclusively from the environment.

Secrets never carry a default value: an unset key stays ``None`` and the
feature that needs it fails loudly instead of silently using a placeholder.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: Port 5436 is the docker-compose stack. A local PostgreSQL on 5432 would
#: answer too, but without pgvector — a confusing failure much later.
DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5436/offers"


class ModelPricing(BaseModel):
    """USD per million tokens for one model.

    Prices live in configuration rather than in the call site so that a price
    change is a deploy, not a code change, and so a model missing from the
    table is visible as an unpriced call instead of a wrong number.
    """

    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    cache_read_usd_per_mtok: float = Field(default=0.0, ge=0)
    cache_write_usd_per_mtok: float = Field(default=0.0, ge=0)


def default_pricing() -> dict[str, ModelPricing]:
    """Anthropic list prices, as published on 2026-09-03.

    Override with the LLM_PRICING environment variable (JSON) rather than
    editing this; the defaults exist so a fresh checkout reports a real cost.
    """
    return {
        "claude-opus-5": ModelPricing(
            input_usd_per_mtok=5.0,
            output_usd_per_mtok=25.0,
            cache_read_usd_per_mtok=0.50,
            cache_write_usd_per_mtok=6.25,
        ),
        "claude-sonnet-5": ModelPricing(
            input_usd_per_mtok=2.0,
            output_usd_per_mtok=10.0,
            cache_read_usd_per_mtok=0.20,
            cache_write_usd_per_mtok=2.50,
        ),
        "claude-haiku-4-5": ModelPricing(
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            cache_read_usd_per_mtok=0.10,
            cache_write_usd_per_mtok=1.25,
        ),
    }


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
    # NoDecode is load-bearing. pydantic-settings JSON-decodes complex fields
    # coming from a .env file BEFORE any validator runs, so the natural
    # `CORS_ORIGINS=http://localhost:5173` raises a JSONDecodeError at startup
    # while the same value passed to Settings(...) directly works fine — which
    # is why the tests missed it. NoDecode hands the raw string to the
    # comma-splitting validator below instead.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # ── LLM ───────────────────────────────────────────────────────────
    anthropic_api_key: SecretStr | None = None
    #: Hot path: vacancy re-rank, Telegram post parsing. Runs thousands of times.
    anthropic_model: str = "claude-sonnet-5"
    #: Cold path: resume extraction, cover letters. Runs rarely, quality matters.
    anthropic_model_heavy: str = "claude-opus-5"
    llm_rerank_top_n: Annotated[int, Field(ge=1, le=200)] = 30
    llm_max_cost_per_run_usd: Annotated[float, Field(ge=0)] = 2.0
    anthropic_max_tokens: Annotated[int, Field(ge=1, le=128_000)] = 16_000
    anthropic_timeout: Annotated[float, Field(gt=0)] = 120.0
    anthropic_max_retries: Annotated[int, Field(ge=0, le=10)] = 4
    #: USD per million tokens, keyed by model id. See ModelPricing.
    llm_pricing: dict[str, ModelPricing] = Field(default_factory=default_pricing)

    # ── Embeddings ────────────────────────────────────────────────────
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: Annotated[int, Field(ge=1)] = 1024
    #: "bge-m3" needs the [embeddings] extra; "fake" is a deterministic stub
    #: used by tests and by anyone who does not want 3.8 GB on disk.
    embedding_provider: Literal["bge-m3", "fake"] = "bge-m3"
    embedding_batch_size: Annotated[int, Field(ge=1, le=256)] = 16
    #: Vectors are cached on disk by sha256(text + model). Without it a full
    #: pipeline run re-encodes thousands of unchanged vacancies.
    embedding_cache_dir: Path | None = Path(".cache/embeddings")

    # ── HTTP ──────────────────────────────────────────────────────────
    http_timeout: Annotated[float, Field(gt=0)] = 30.0
    user_agent: str = "who-wants-an-offer/1.0 (+https://github.com/nurzhan2/Who_wants_an_offer-)"
    #: Set in dev to cache source responses on disk and stop hammering APIs.
    http_cache_dir: Path | None = None

    # ── Resume upload ─────────────────────────────────────────────────
    resume_max_file_size_mb: Annotated[int, Field(ge=1, le=100)] = 10
    #: A profile stuck in "pending" longer than this is reported as failed:
    #: background tasks do not survive a restart.
    resume_parse_timeout_seconds: Annotated[int, Field(ge=30)] = 900
    #: Uploads are written here while the background task parses them, then
    #: deleted. Gitignored; swept at startup for files a crash left behind.
    upload_dir: Path = Path("uploads")

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

    @field_validator("llm_pricing", mode="before")
    @classmethod
    def _blank_pricing_means_the_defaults(cls, value: Any) -> Any:
        """An empty LLM_PRICING falls back to the built-in table.

        ``_empty_string_is_unset`` turns a blank env var into None, which is
        right for an optional field and fatal for one with a default: the
        shipped .env.example carried ``LLM_PRICING=`` and copying it verbatim —
        exactly what the README tells you to do — made the app refuse to start.
        """
        return default_pricing() if value is None else value

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
