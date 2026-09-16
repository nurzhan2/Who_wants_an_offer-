"""Application settings, loaded exclusively from the environment.

Secrets never carry a default value: an unset key stays ``None`` and the
feature that needs it fails loudly instead of silently using a placeholder.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.llm.base import Effort, LLMTask

Environment = Literal["development", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: Port 5436 is the docker-compose stack. A local PostgreSQL on 5432 would
#: answer too, but without pgvector — a confusing failure much later.
DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5436/offers"

#: Providers the router knows how to build. A routing entry naming anything
#: else is a configuration error, caught at startup rather than on the one call
#: that needed it.
PROVIDER_NAMES: frozenset[str] = frozenset({"api", "cli", "ollama"})


def default_routing() -> dict[str, str]:
    """Which provider serves which task.

    The split is arithmetic, not taste. A Claude Code CLI call carries about
    50,000 tokens of its own system prompt whatever the payload — measured at
    $0.064 for a prompt whose answer is ``{"ok": true}`` — so the tasks that run
    once per upload go there and the ones that run hundreds of times a day do
    not. Re-rank alone, at 120 calls a day, would be roughly $44 a day of
    subscription quota before a single useful token.
    """
    return {
        LLMTask.RESUME_EXTRACTION.value: "cli",
        LLMTask.COVER_LETTER.value: "cli",
        LLMTask.CV_TAILORING.value: "cli",
        LLMTask.TOOLING.value: "cli",
        LLMTask.TELEGRAM_PARSE.value: "ollama",
        LLMTask.VACANCY_PARSE.value: "api",
        LLMTask.RERANK.value: "api",
    }


def default_fallback_chain() -> dict[str, list[str]]:
    """Where a call goes when its provider cannot serve it.

    The API is the terminus: it is the only provider available whenever a key
    is configured. Every fallback is logged at WARNING — a quiet switch would
    put two extraction qualities in the same dataset with no way to tell which
    rows came from which.
    """
    return {"cli": ["api"], "ollama": ["api"], "api": []}


def default_task_effort() -> dict[str, Effort]:
    """How hard to think, per task.

    One global setting would either overspend on the hot path or underthink on
    the cold one: re-rank runs 120 times a day, resume extraction runs once.
    """
    return {
        LLMTask.RESUME_EXTRACTION.value: "high",
        LLMTask.COVER_LETTER.value: "high",
        LLMTask.CV_TAILORING.value: "high",
        LLMTask.TOOLING.value: "high",
        LLMTask.VACANCY_PARSE.value: "medium",
        LLMTask.TELEGRAM_PARSE.value: "low",
        LLMTask.RERANK.value: "low",
    }


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
    #: task -> provider. Moving a task between providers is configuration.
    llm_routing: dict[str, str] = Field(default_factory=default_routing)
    #: provider -> ordered fallbacks. An empty list means "fail, do not move".
    llm_fallback_chain: dict[str, list[str]] = Field(default_factory=default_fallback_chain)
    #: task -> effort.
    llm_task_effort: dict[str, Effort] = Field(default_factory=default_task_effort)

    # ── Claude Code CLI provider ──────────────────────────────────────
    #: Resolved from PATH once at startup when unset. An explicit path wins,
    #: which is how a machine with several installs picks one.
    claude_cli_binary: str | None = None
    claude_cli_timeout: Annotated[float, Field(gt=0)] = 300.0
    #: Reading a file costs a turn, so a task that reads a PDF needs more than
    #: one. Too few and the call dies against the limit having paid in full.
    claude_cli_max_turns: Annotated[int, Field(ge=1, le=20)] = 6
    #: A local process, not a server. Two at a time is plenty.
    claude_cli_concurrency: Annotated[int, Field(ge=1, le=8)] = 2

    # ── Ollama provider ───────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_timeout: Annotated[float, Field(gt=0)] = 180.0

    #: Two-stage re-rank: a cheap pass over everything, an expensive pass over
    #: what survives. Wired in phase 5; the models live here so switching it on
    #: is configuration rather than a code change.
    rerank_stage_models: list[str] = Field(
        default_factory=lambda: ["claude-haiku-4-5", "claude-sonnet-5"]
    )

    # ── Embeddings ────────────────────────────────────────────────────
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: Annotated[int, Field(ge=1)] = 1024
    #: "bge-m3" needs the [embeddings] extra; "fake" is a deterministic stub
    #: used by tests and by anyone who does not want 3.8 GB on disk.
    embedding_provider: Literal["bge-m3", "fake"] = "bge-m3"
    embedding_batch_size: Annotated[int, Field(ge=1, le=256)] = 16
    #: Vectors one run may compute. Measured on a CPU-only machine bge-m3 costs
    #: about seven seconds a posting, so this is really the wall clock the
    #: operator is willing to spend, expressed in rows. It replaces a hardcoded
    #: 500 and is deliberately higher than it: the step now commits every batch,
    #: so a long run is no longer all-or-nothing and a bigger cap risks nothing.
    #: Checked between batches, so a run may overshoot it by at most one batch —
    #: stopping inside a batch would discard vectors already computed, which is
    #: the exact loss the step was rewritten to prevent.
    embedding_max_per_run: Annotated[int, Field(ge=1)] = 2000
    #: Wall clock one run may spend embedding, checked between batches so a batch
    #: is never abandoned half-computed. Needed alongside the count because
    #: neither bounds the other: a row served from the disk cache costs
    #: microseconds and a cold one costs seconds.
    embedding_time_budget_seconds: Annotated[float, Field(gt=0)] = 3600.0
    #: Vectors are cached on disk by sha256(text + model). Without it a full
    #: pipeline run re-encodes thousands of unchanged vacancies.
    embedding_cache_dir: Path | None = Path(".cache/embeddings")

    # ── HTTP ──────────────────────────────────────────────────────────
    http_timeout: Annotated[float, Field(gt=0)] = 30.0
    user_agent: str = "who-wants-an-offer/1.0 (+https://github.com/nurzhan2/Who_wants_an_offer-)"
    #: Set in dev to cache source responses on disk and stop hammering APIs.
    http_cache_dir: Path | None = None

    # ── ATS readability audit ─────────────────────────────────────────
    # Thresholds for modelling a dumb parser. They are configuration because
    # they are judgement calls calibrated against real files, not constants.
    #: A gap wider than this share of the page width separates two columns.
    ats_column_gap_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.06
    #: A band must hold this share of the page's words to count as a column,
    #: so a page number in a corner is not a second column.
    ats_min_column_share: Annotated[float, Field(gt=0, lt=1)] = 0.08
    #: Above this share of lines reading across columns, extraction is broken.
    ats_mixed_line_ratio: Annotated[float, Field(gt=0, le=1)] = 0.15
    #: Below this many characters, the file has no usable text layer.
    ats_min_text_chars: Annotated[int, Field(ge=0)] = 200
    #: Above this share of unrecognisable characters, the font did not survive.
    ats_broken_glyph_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.05
    #: Above this share of words inside tables, sectioning is at risk.
    ats_table_word_ratio: Annotated[float, Field(gt=0, le=1)] = 0.30
    #: At or above this lightness, a character is the colour of the paper. 0.9
    #: rather than 1.0 because "#fefefe" is the spelling that gets used when
    #: somebody knows a checker is looking for pure white.
    ats_invisible_luminance: Annotated[float, Field(gt=0, le=1)] = 0.90
    #: Below this lightness a filled shape is a dark banner, and light text on
    #: it is a design rather than a hidden block.
    ats_dark_fill_luminance: Annotated[float, Field(ge=0, lt=1)] = 0.50
    #: Points below which text is not meant to be read by a person. Smaller than
    #: any real footnote: 6pt fine print exists, 3pt does not.
    ats_min_font_size: Annotated[float, Field(gt=0)] = 4.0
    #: Invisible characters below this count are an artefact; at or above it
    #: they are a block somebody placed. One word is about this long.
    ats_hidden_char_limit: Annotated[int, Field(ge=1)] = 20
    #: A resume shorter than this has nothing for a keyword filter to match.
    #: Measured rather than guessed: a complete but terse one-page resume in
    #: this project's fixture corpus runs 131 to 204 words, so a floor of 150
    #: reported finished resumes as too short. Below 100 a document cannot be
    #: carrying work history, skills and education at all.
    ats_min_resume_words: Annotated[int, Field(ge=0)] = 100
    #: Longer than this and the import truncates — always from the end, which is
    #: where the early career and the education live.
    ats_max_resume_words: Annotated[int, Field(ge=1)] = 1200

    # ── Resume upload ─────────────────────────────────────────────────
    resume_max_file_size_mb: Annotated[int, Field(ge=1, le=100)] = 10
    #: A profile stuck in "pending" longer than this is reported as failed:
    #: background tasks do not survive a restart.
    resume_parse_timeout_seconds: Annotated[int, Field(ge=30)] = 900
    #: Uploads are written here while the background task parses them, then
    #: deleted. Gitignored; swept at startup for files a crash left behind.
    upload_dir: Path = Path("uploads")

    # ── Sources: framework ────────────────────────────────────────────
    # Source-agnostic on purpose. A per-source flag (HH_ENABLED, JSEARCH_ENABLED)
    # would mean editing this file for every new connector, and CLAUDE.md rule 5
    # says adding a source must not require changes outside sources/.
    #: Slugs switched off for this deployment. Comma-separated.
    sources_disabled: Annotated[frozenset[str], NoDecode] = frozenset()
    #: When non-empty, ONLY these slugs run. For a debugging session.
    sources_enabled: Annotated[frozenset[str], NoDecode] = frozenset()
    #: Per-source credentials, keyed "<slug>.<name>" — a connector declares the
    #: keys it needs and the default is_configured() answers without any
    #: framework code. Never logged, in any form: see app/sources/http.py.
    #: NoDecode for the same reason cors_origins needs it, and this is the third
    #: time this bug has been fixed here: pydantic-settings JSON-decodes a
    #: complex field coming from a .env file BEFORE any validator runs, so a
    #: blank ``SOURCE_CREDENTIALS=`` — which is what .env.example ships and the
    #: README tells you to copy — raised a JSONDecodeError at import and the app
    #: would not start. NoDecode hands the raw string to the validator below.
    source_credentials: Annotated[dict[str, SecretStr], NoDecode] = Field(default_factory=dict)
    #: How long a cached source response stays fresh when the connector does
    #: not override it. Only consulted when http_cache_dir is set.
    http_cache_ttl_seconds: Annotated[int, Field(ge=0)] = 3600
    #: Ceiling on the queries one pipeline run may issue per source. Enforced by
    #: truncation in the planner, not by a warning: a plan that is merely
    #: advised to be small is a plan that grows.
    max_queries_per_run: Annotated[int, Field(ge=1, le=200)] = 8
    #: Largest batch of external ids sent in one "which of these do we know?"
    #: lookup. Without a cap a long run builds a multi-thousand IN clause.
    external_id_lookup_batch: Annotated[int, Field(ge=1, le=5000)] = 500

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

    # ── Local apply agent (agent/) ────────────────────────────────────
    # The agent runs on the owner's own machine, under their own hh account,
    # and reaches this API over HTTP and nothing else. It has no database
    # access. See app/api/v1/applications.py for why the authentication here
    # is one local token and deliberately nothing more.
    #: Shared secret for /api/v1/applications. No default, like every other
    #: secret here: unset means the queue refuses to serve, never that it
    #: serves without a check.
    agent_api_token: SecretStr | None = None
    #: Which connector's postings the agent can act on. Configuration rather
    #: than a constant so that teaching the agent a second site does not need
    #: an edit in this file — the same reason CLAUDE.md rule 5 gives for
    #: keeping per-source knowledge out of the framework.
    agent_source_slug: str = "hh"
    #: Floor on the match score before a vacancy is worth a slot out of the
    #: agent's deliberately small daily budget. The floor of ``strong`` on the
    #: title formula (``rules.TITLE_BUCKETS``), as 70 was on the component one:
    #: on the corpus measured 13 Sep 2026, 70 lets 1129 of 1355 unfiltered
    #: vacancies through under the title formula and 78 lets 78.
    agent_queue_min_score: Annotated[int, Field(ge=0, le=100)] = 78
    #: How long a confirmation given on the dashboard's vacancy card stays good.
    #: A third of the first real queue (16 Sep 2026) was archived by the time
    #: the agent reached it; a "yes" given days ago was given about a page that
    #: may no longer exist, so it expires and the owner is asked again.
    agent_confirmation_ttl_hours: Annotated[int, Field(ge=1, le=720)] = 72

    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, value: Any) -> Any:
        """Treat ``KEY=`` in a .env file as "not configured", not as an empty value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sources_disabled", "sources_enabled", mode="before")
    @classmethod
    def _split_source_slugs(cls, value: Any) -> Any:
        """Accept a comma-separated list, which is what an env var holds.

        Also accepts None, because ``_empty_string_is_unset`` turns a blank
        ``SOURCES_DISABLED=`` into it, and an empty deny-list is the sane
        reading of an empty value rather than a boot failure.
        """
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return frozenset(slug.strip() for slug in value.split(",") if slug.strip())
        return value

    @field_validator("source_credentials", mode="before")
    @classmethod
    def _parse_credentials(cls, value: Any) -> Any:
        """Decode the JSON ourselves, and read a blank value as "none set".

        Both halves matter. NoDecode above means the JSON arrives as a string
        and nobody else will parse it; and an absent value has to mean an empty
        mapping rather than None, because None is not a dict and pydantic would
        refuse to build the settings at all.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "SOURCE_CREDENTIALS must be a JSON object keyed "
                    f'"<slug>.<name>", e.g. {{"jsearch.rapidapi_key": "..."}}: {exc}'
                ) from exc
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

    @model_validator(mode="after")
    def _routing_is_complete_and_terminating(self) -> "Settings":
        """Reject a routing table that cannot answer some call.

        Every failure here is a startup error rather than a surprise on the one
        task nobody exercised: a task with no route, a provider that does not
        exist, or a fallback chain that loops instead of reaching a terminus.
        """
        missing = sorted({task.value for task in LLMTask} - set(self.llm_routing))
        if missing:
            raise ValueError(f"LLM_ROUTING does not cover: {missing}")

        unknown = sorted(set(self.llm_routing.values()) - PROVIDER_NAMES)
        if unknown:
            raise ValueError(f"LLM_ROUTING names unknown providers: {unknown}")

        named = set(self.llm_fallback_chain) | {
            name for chain in self.llm_fallback_chain.values() for name in chain
        }
        unknown_chain = sorted(named - PROVIDER_NAMES)
        if unknown_chain:
            raise ValueError(f"LLM_FALLBACK_CHAIN names unknown providers: {unknown_chain}")

        for start in self.llm_fallback_chain:
            seen = {start}
            current = start
            while chain := self.llm_fallback_chain.get(current):
                current = chain[0]
                if current in seen:
                    raise ValueError(f"LLM_FALLBACK_CHAIN loops through {current!r}")
                seen.add(current)

        missing_effort = sorted({task.value for task in LLMTask} - set(self.llm_task_effort))
        if missing_effort:
            raise ValueError(f"LLM_TASK_EFFORT does not cover: {missing_effort}")
        return self

    def provider_for(self, task: LLMTask) -> str:
        """Which provider a task is routed to."""
        return self.llm_routing[task.value]

    def effort_for(self, task: LLMTask) -> Effort:
        """How hard a task should think."""
        return self.llm_task_effort[task.value]

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
