"""Settings are environment-driven and free of baked-in secrets."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DEV_DATABASE_URL, Settings

#: Every field that may hold a credential. None of them may have a default.
SECRET_FIELDS = (
    "anthropic_api_key",
    "adzuna_app_key",
    "jooble_api_key",
    "rapidapi_key",
    "themuse_api_key",
    "findwork_token",
    "telegram_api_hash",
    "telegram_bot_token",
)


def _settings(**env: str) -> Settings:
    """Build settings from an explicit environment, ignoring any local .env file."""
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_no_secret_has_a_default(field: str) -> None:
    """An unset credential is None, never a placeholder value."""
    assert getattr(_settings(), field) is None


def test_values_come_from_the_environment() -> None:
    """Scalar settings are read from the environment, case-insensitively."""
    settings = _settings(
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://u:p@db:5432/offers",
        ANTHROPIC_API_KEY="sk-test",
        LLM_RERANK_TOP_N="15",
        HTTP_TIMEOUT="7.5",
    )

    assert settings.environment == "production"
    assert settings.is_production is True
    assert settings.llm_rerank_top_n == 15
    assert settings.http_timeout == 7.5
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test"


def test_secrets_are_masked_in_repr() -> None:
    """A settings dump must not leak a key into logs or a traceback."""
    settings = _settings(ANTHROPIC_API_KEY="sk-super-secret")

    assert "sk-super-secret" not in repr(settings)


def test_model_split_between_hot_and_cold_paths() -> None:
    """Re-rank runs on the cheap model; resume parsing on the strong one."""
    settings = _settings()

    assert settings.anthropic_model == "claude-sonnet-5"
    assert settings.anthropic_model_heavy == "claude-opus-5"
    assert settings.anthropic_model != settings.anthropic_model_heavy


def test_production_rejects_development_fallbacks() -> None:
    """Booting production without explicit config fails loudly at startup."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(ENVIRONMENT="production")

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "ANTHROPIC_API_KEY" in message


def test_development_uses_local_database_by_default() -> None:
    """Development boots without a .env file at all."""
    assert _settings().database_url == DEV_DATABASE_URL


def test_empty_env_var_is_treated_as_unset() -> None:
    """`ADZUNA_APP_ID=` in a .env file means "not configured", not an empty string."""
    settings = _settings(ADZUNA_APP_ID="", HTTP_CACHE_DIR="")

    assert settings.adzuna_app_id is None
    assert settings.http_cache_dir is None


def test_cors_origins_parsed_from_comma_separated_list() -> None:
    """Env vars are flat strings; the list form must survive that."""
    settings = _settings(CORS_ORIGINS="http://localhost:5173, https://offers.example")

    assert settings.cors_origins == ["http://localhost:5173", "https://offers.example"]


def test_paths_are_parsed_as_paths() -> None:
    """Path-typed settings arrive as Path, not str."""
    settings = _settings(TELEGRAM_SESSION_PATH="/data/tg.session")

    assert isinstance(settings.telegram_session_path, Path)


def test_out_of_range_values_are_rejected() -> None:
    """Bounds live in the schema, so a typo in .env fails fast."""
    with pytest.raises(ValidationError):
        _settings(MATCH_SCORE_ALERT_THRESHOLD="500")


def test_the_shipped_env_example_produces_valid_settings() -> None:
    """The README says to copy .env.example to .env. Doing that has to work.

    It once did not: the file shipped ``LLM_PRICING=`` and a blank value for a
    field with a default is a validation error, so a fresh checkout followed
    exactly as documented refused to start. Nothing else checks this file.
    """
    example = Path(__file__).resolve().parents[2] / ".env.example"

    settings = Settings(_env_file=example)  # type: ignore[call-arg]

    assert settings.llm_pricing
    assert settings.environment == "development"


def test_a_schedule_that_is_not_a_time_of_day_is_refused_at_boot() -> None:
    """The alternative is discovering it at 03:00, in a process nobody is watching.

    ``AUTOPILOT_DAILY_AT`` is read once when the loop is armed, so an
    unparseable value would not raise until the first wake-up — by which point
    the chain has silently not run for a day and the traceback is in a log.
    """
    for bad in ("3pm", "25:00", "ночью"):
        with pytest.raises(ValidationError):
            _settings(AUTOPILOT_DAILY_AT=bad)


def test_an_empty_schedule_means_no_schedule_rather_than_an_error() -> None:
    """``AUTOPILOT_DAILY_AT=`` in a copied .env is "I do not want one"."""
    assert _settings(AUTOPILOT_DAILY_AT="").autopilot_daily_at is None
    assert _settings(AUTOPILOT_DAILY_AT="03:30").autopilot_daily_at == "03:30"
