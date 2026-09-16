"""Development seed data for the dashboard.

Fills an already-migrated database with enough shape that every visual state of
the frontend has something real to render: all five match buckets, postings
with no salary, postings with no publication date, and the cross-posted case
where a single vacancy carries two source rows.

**Idempotent.** Everything written here has a stable natural key, so a second
run updates the same rows instead of adding new ones:

* vacancies are upserted on ``fingerprint``, derived from a fixed seed string
  rather than from anything random;
* source rows are upserted on ``(source_slug, external_id)``, both derived from
  that same seed string;
* matches are upserted on ``(profile_id, vacancy_id)``;
* the profile and the tracker entries carry hardcoded UUIDs and are inserted
  only when that id is not in the table yet.

**Deterministic.** No randomness and no wall clock: every timestamp is an
offset from :data:`SEED_EPOCH`, so two runs on two machines produce identical
rows and a screenshot taken today still matches one taken next month.

Usage::

    make seed          # or: uv run python scripts/seed.py

This script never creates the schema. Run ``make migrate`` first.
"""

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.enums import (
    ApplicationStatus,
    EmploymentType,
    MatchBucket,
    ParseStatus,
    PipelineRunStatus,
    RemoteType,
    RuleScope,
    SalaryPeriod,
    Seniority,
    SkillLevel,
)
from app.db.models import (
    Application,
    CandidateProfile,
    PipelineRun,
    ProfileSkill,
    Vacancy,
)
from app.db.repositories import (
    MatchRepository,
    ProfileRepository,
    SourceStateRepository,
    VacancyRepository,
)
from app.db.seed_rows import SEED_URL_PREFIX
from app.documents import rules as document_rules
from app.schemas.ats import (
    ATSCoverage,
    ATSReport,
    Finding,
    FindingCode,
    Recoverable,
    Severity,
)
from app.schemas.match import MatchComponentScores, MatchCreate, MatchedSkill, MissingSkill
from app.schemas.profile import CandidateProfileCreate, SkillCreate
from app.schemas.vacancy import VacancyCreate
from app.sources.hh import (
    CENSUS_PREFIX,
    POSITION_PREFIX,
    FileCensus,
    FileWatermark,
    HHSite,
    Span,
)
from app.workshop.rules import BUILTIN_RULES

logger = structlog.get_logger(__name__)

#: Every timestamp in the seed is an offset from this instant. Fixed on purpose:
#: a seed that moves with the clock makes yesterday's screenshots un-reproducible.
SEED_EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

CENTS = Decimal("0.01")

#: Stable ids, so the seeded profile and tracker entries keep the same URLs
#: across runs and a second run recognises them instead of duplicating them.
SEED_PROFILE_ID = UUID("0192f000-0000-7000-8000-000000000001")

VACANCY_COUNT = 60

#: The connectors the dashboard filters by.
SOURCES: tuple[str, ...] = ("hh", "telegram", "jsearch", "remotive")

#: Vacancies that exist on two boards at once: one ``vacancy`` row, two
#: ``vacancy_source`` rows. The dashboard has to render that badge and it is
#: the easiest case to forget, so the seed always contains some.
CROSS_POSTED_INDEXES: tuple[int, ...] = (5, 15, 25, 35, 45)

#: Postings with no salary at all, so the frontend meets that empty state.
NO_SALARY_MODULUS = 7
NO_SALARY_REMAINDER = 3

#: Postings with no publication date, which is also the NULL tail every
#: keyset-paginated "sort by published_at" query has to order last.
NO_PUBLISHED_AT_MODULUS = 11
NO_PUBLISHED_AT_REMAINDER = 5

#: Postings advertised per year rather than per month, to exercise the period
#: conversion in the normalisation below.
YEARLY_MODULUS = 9
YEARLY_REMAINDER = 4

TITLES: tuple[str, ...] = (
    "Backend Engineer",
    "Python Developer",
    "Data Engineer",
    "Platform Engineer",
    "Senior Backend Developer",
    "ML Engineer",
)

#: The rules a seeded letter is recorded as written under: the built-in guard
#: and the workshop's two undeletable rules, which is what a fresh database has.
#: Computed rather than written down, so seeded rows carry the value the writer
#: would have written.
LETTER_RULES_VERSION = document_rules.version(
    tuple(rule for rule in BUILTIN_RULES if rule.applies_to(RuleScope.COVER_LETTER)),
    scope=RuleScope.COVER_LETTER,
)

COMPANIES: tuple[str, ...] = (
    "Acme Labs",
    "Nomad Tech",
    "Steppe Systems",
    "Orbita Digital",
    "Baiterek Software",
    "Tulpar Analytics",
    "Kolibri Cloud",
)

#: The last entry is the location-less one, matched by ``RemoteType.FULL`` in
#: :data:`REMOTE_CYCLE` so a fully remote posting is the one without a city.
CITIES: tuple[tuple[str | None, str | None], ...] = (
    ("Алматы", "KZ"),
    ("Астана", "KZ"),
    ("Тбилиси", "GE"),
    ("Варшава", "PL"),
    (None, None),
)

REMOTE_CYCLE: tuple[RemoteType, ...] = (
    RemoteType.NO,
    RemoteType.HYBRID,
    RemoteType.NO,
    RemoteType.HYBRID,
    RemoteType.FULL,
)

SENIORITIES: tuple[Seniority, ...] = (
    Seniority.JUNIOR,
    Seniority.MIDDLE,
    Seniority.SENIOR,
    Seniority.LEAD,
)

CURRENCIES: tuple[str, ...] = ("KZT", "USD", "EUR", "PLN")

#: Plausible monthly bands per currency, before the per-index spread.
SALARY_BANDS: dict[str, tuple[Decimal, Decimal]] = {
    "KZT": (Decimal("600000.00"), Decimal("1100000.00")),
    "USD": (Decimal("3500.00"), Decimal("6000.00")),
    "EUR": (Decimal("3800.00"), Decimal("6200.00")),
    "PLN": (Decimal("15000.00"), Decimal("24000.00")),
}

# ── development salary normalisation ──────────────────────────────────
#
# DEVELOPMENT STAND-IN. The real normalisation phase will fetch rates and
# record when it ran; until it lands, the dashboard still has to sort by
# salary, and sorting on the advertised amount ranks 600000 KZT above
# 4000 USD. These rates are hardcoded, approximate and never refreshed —
# they exist so the seeded data sorts sensibly, not so anything is accurate.
# Delete this table the day app/normalize/ owns the conversion.

#: USD per one unit of the currency.
DEV_USD_RATES: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.09"),
    "KZT": Decimal("0.0021"),
    "PLN": Decimal("0.25"),
}

#: How many of each period fit into one month, to get a monthly amount.
PERIOD_TO_MONTH: dict[SalaryPeriod, Decimal] = {
    SalaryPeriod.HOUR: Decimal("160"),
    SalaryPeriod.DAY: Decimal("21"),
    SalaryPeriod.MONTH: Decimal("1"),
    SalaryPeriod.YEAR: Decimal("1") / Decimal("12"),
}

# ── the candidate ─────────────────────────────────────────────────────

#: Name of the seeded candidate. A constant because it is the only thing that
#: identifies the profile apart from its id, and re-seeding has to be able to
#: tell "the row I wrote last time" from "a profile a real resume produced".
PROFILE_NAME = "Разработчик Разработчикович"

#: Twenty canonicalised skills — enough that the match explanation panel has
#: something to fold, and enough overlap to make every bucket believable.
PROFILE_SKILLS: tuple[str, ...] = (
    "python",
    "fastapi",
    "django",
    "sqlalchemy",
    "postgresql",
    "redis",
    "docker",
    "kubernetes",
    "aws",
    "terraform",
    "celery",
    "rabbitmq",
    "pytest",
    "git",
    "linux",
    "asyncio",
    "pydantic",
    "alembic",
    "grafana",
    "kafka",
)

#: Skills no seeded vacancy match ever covers, so "missing required" is never
#: empty for the weaker buckets.
#:
#: **Canonical names, not spellings.** ``app/services/vacancies.py`` decides
#: whether an uncovered requirement is one the candidate actually lacks by
#: looking that name up — in the other resume's skill rows, and in this one's
#: text through the same canonicaliser the extractor uses. "golang" and "hadoop"
#: are not canonical names in ``skills_min.yaml`` (``go`` is; hadoop is absent),
#: so a seed using them would produce a card where every requirement is absent
#: and the middle case could never be seen.
#:
#: The four are chosen so that all three columns of that card have something in
#: them: ``spark`` is listed by the older resume, ``rust`` is named in this
#: resume's text and was never extracted into a skill row, and ``scala`` and
#: ``go`` are genuinely absent.
MISSING_POOL: tuple[str, ...] = ("spark", "rust", "scala", "go")

#: Named in the active resume's text, deliberately absent from its skill rows:
#: the "extraction missed it" evidence. Must be a spelling the dictionary knows.
MENTIONED_NOT_EXTRACTED = "Rust"

#: The skill the older resume claims and this one does not mention at all.
CLAIMED_BY_OLDER_RESUME = "spark"

# ── scoring ───────────────────────────────────────────────────────────

#: (bucket, how many vacancies land in it, base score). The counts add up to
#: VACANCY_COUNT and every bucket is present, including ``filtered``, which the
#: dashboard hides by default and which therefore has to be seeded explicitly
#: or the "show filtered" toggle has nothing to show.
BUCKET_PLAN: tuple[tuple[MatchBucket, int, str], ...] = (
    (MatchBucket.APPLY_NOW, 8, "92.00"),
    (MatchBucket.STRONG, 14, "77.50"),
    (MatchBucket.STRETCH, 16, "61.00"),
    (MatchBucket.SKIP, 16, "42.00"),
    (MatchBucket.FILTERED, 6, "18.00"),
)

MISSING_BY_BUCKET: dict[MatchBucket, int] = {
    MatchBucket.APPLY_NOW: 0,
    MatchBucket.STRONG: 1,
    MatchBucket.STRETCH: 2,
    MatchBucket.SKIP: 3,
    MatchBucket.FILTERED: 4,
}

VERDICT_BY_BUCKET: dict[MatchBucket, str] = {
    MatchBucket.APPLY_NOW: "Стек совпадает почти полностью — откликаться сегодня.",
    MatchBucket.STRONG: "Хорошее совпадение, не хватает одного требования.",
    MatchBucket.STRETCH: "Дотянуться можно, но придётся объяснить два пробела.",
    MatchBucket.SKIP: "Слишком много незакрытых требований.",
    MatchBucket.FILTERED: "Отсеяно жёстким фильтром: стек не тот.",
}

# ── the tracker ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SeededApplication:
    """One tracker row, with everything a send would have recorded on it.

    A dataclass rather than another tuple because the row now carries three
    separate groups of fields — the person's, the agent's report, hh's own
    words — and a nine-element tuple is where a seed starts writing hh's
    warning into the agent's reason without anybody noticing.
    """

    id: UUID
    vacancy_index: int
    status: ApplicationStatus
    #: Days before the epoch the *person* dated it. None means they never did.
    days_ago: int | None = None
    #: The agent's own state machine, or None for a row typed in by hand.
    agent_status: str | None = None
    agent_reason: str | None = None
    letter: str | None = None
    #: True when the agent reported an actual send. Only then are ``sent_at``
    #: and ``sent_letter`` written, because those are the evidence that one
    #: happened and inventing them here would make the counters unusable.
    sent: bool = False
    hh_warning: str | None = None
    hh_blocking_warning: str | None = None
    hh_negotiations_total: int | None = None
    hh_last_state: str | None = None
    #: Which version of the letter rules judged the stored letter. None for a
    #: letter written before the version was recorded, which is a real state and
    #: has to render as "not recorded" rather than as version zero.
    rules_version: str | None = None


#: A letter of the shape the generator actually produces: no links, no at-sign,
#: over the two-hundred-character floor the guard applies.
CLEAN_LETTER = (
    "Здравствуйте! Меня заинтересовала ваша вакансия. Последние семь лет пишу "
    "бэкенд на Python: асинхронные сервисы на FastAPI, SQLAlchemy и PostgreSQL, "
    "очереди задач на Celery и RabbitMQ, эксплуатация в Docker и Kubernetes. "
    "Собирал пайплайны обработки событий и отвечал за их надёжность под "
    "нагрузкой. Буду рад рассказать подробнее о том, как это устроено, и "
    "обсудить, чем могу быть полезен вашей команде."
)

#: The same letter after somebody added a link by hand. hh filters letters with
#: addresses, so today's rules reject it — and the documents screen exists partly
#: to make that visible on a letter that was saved when it passed.
EDITED_LETTER = CLEAN_LETTER + " Примеры работ: github.com/example/portfolio"


#: Every column of the applications board has to have something in it, and the
#: interesting ones are the two that are easy to leave empty: a row waiting for
#: a person, with the reason the agent wrote, and a row hh has answered.
APPLICATIONS: tuple[SeededApplication, ...] = (
    SeededApplication(
        id=UUID("0192f000-0000-7000-8000-00000000a001"),
        vacancy_index=0,
        status=ApplicationStatus.APPLIED,
        days_ago=6,
        agent_status="sent",
        letter=CLEAN_LETTER,
        sent=True,
        hh_warning="Такой отклик может получить отказ: не указан опыт работы с Kafka.",
        hh_negotiations_total=1,
        hh_last_state="RESPONSE",
        rules_version=LETTER_RULES_VERSION,
    ),
    SeededApplication(
        id=UUID("0192f000-0000-7000-8000-00000000a002"),
        vacancy_index=2,
        status=ApplicationStatus.INTERVIEW,
        days_ago=14,
        agent_status="sent",
        letter=EDITED_LETTER,
        sent=True,
        hh_blocking_warning="Резюме скрыто от работодателей — они не увидят его целиком.",
        hh_negotiations_total=1,
        hh_last_state="INVITATION",
        # None on purpose: written before the version was recorded. The
        # documents screen has to say "не записана" rather than invent a zero.
        rules_version=None,
    ),
    SeededApplication(
        id=UUID("0192f000-0000-7000-8000-00000000a003"),
        vacancy_index=5,
        status=ApplicationStatus.SAVED,
        agent_status="queued",
        letter=CLEAN_LETTER,
        rules_version=LETTER_RULES_VERSION,
    ),
    SeededApplication(
        id=UUID("0192f000-0000-7000-8000-00000000a004"),
        vacancy_index=1,
        status=ApplicationStatus.SAVED,
        agent_status="needs_manual",
        agent_reason="работодатель требует пройти тест перед откликом",
        letter=CLEAN_LETTER,
        rules_version=LETTER_RULES_VERSION,
    ),
    SeededApplication(
        id=UUID("0192f000-0000-7000-8000-00000000a005"),
        vacancy_index=3,
        status=ApplicationStatus.REJECTED,
        days_ago=21,
        agent_status="sent",
        letter=CLEAN_LETTER,
        sent=True,
        hh_negotiations_total=2,
        hh_last_state="DISCARD",
        rules_version=LETTER_RULES_VERSION,
    ),
    SeededApplication(
        id=UUID("0192f000-0000-7000-8000-00000000a006"),
        vacancy_index=7,
        status=ApplicationStatus.SAVED,
        days_ago=None,
    ),
)

APPLICATION_NOTES: dict[ApplicationStatus, str] = {
    ApplicationStatus.APPLIED: "Отправлено через сайт компании, ответа пока нет.",
    ApplicationStatus.INTERVIEW: "Техническое интервью назначено, готовлю рассказ про пайплайн.",
    ApplicationStatus.SAVED: "Отложено: сначала надо понять, что там по релокации.",
    ApplicationStatus.REJECTED: "Ответили отказом через три недели, без объяснений.",
}


#: The active resume's text layer. Names one technology the skill extractor did
#: not turn into a row — see :data:`MENTIONED_NOT_EXTRACTED` — because that gap
#: is exactly what the vacancy card's middle column is for, and a seed whose
#: text and skill rows agree perfectly can never show it.
RESUME_TEXT = (
    "Seeded development resume. Not extracted from a real file.\n"
    "Бэкенд на Python: FastAPI, SQLAlchemy, PostgreSQL, Redis, Docker.\n"
    f"Пробовал {MENTIONED_NOT_EXTRACTED} в одном сервисе обработки событий, "
    "в основной стек не вошёл."
)

#: The resume before the current one. Inactive, kept, and the only evidence in
#: this database that a skill missing from today's CV is not missing from the
#: candidate: an earlier CV is a claim the person made in writing.
SEED_PREVIOUS_PROFILE_ID = UUID("0192f000-0000-7000-8000-000000000002")
PREVIOUS_PROFILE_SKILLS: tuple[str, ...] = (
    "python",
    "django",
    "postgresql",
    CLAIMED_BY_OLDER_RESUME,
)

#: The readability audit stored with the resume. Degraded rather than clean on
#: purpose: a report with no findings renders as an empty panel, and the panel is
#: only worth building for the case where a parser loses something.
ATS_REPORT = ATSReport(
    score=78,
    findings=[
        Finding(
            code=FindingCode.TEXT_IN_TABLES,
            severity=Severity.WARNING,
            title="Опыт работы свёрстан таблицей",
            explanation=(
                "Парсер читает таблицу по ячейкам, поэтому должность и даты "
                "приезжают из разных строк и перестают быть одной записью."
            ),
            example_fragment="Senior Backend Engineer | 2021 | Acme Labs | Алматы",
            fix="Перевёрстать раздел в один столбец обычным текстом.",
            penalty=12,
        ),
        Finding(
            code=FindingCode.DATES_NOT_EXTRACTABLE,
            severity=Severity.INFO,
            title="Одна из дат записана словами",
            explanation="«с весны 2019» не разбирается в дату, стаж по этой записи не считается.",
            example_fragment="с весны 2019 по настоящее время",
            fix="Писать даты числами: 03.2019 — н. в.",
            penalty=10,
        ),
    ],
    checks_run=[
        FindingCode.NO_TEXT_LAYER,
        FindingCode.COLUMN_INTERLEAVING,
        FindingCode.TEXT_IN_TABLES,
        FindingCode.DATES_NOT_EXTRACTABLE,
        FindingCode.MISSING_SECTIONS,
    ],
    sections_detected=["Опыт работы", "Навыки", "Образование"],
    coverage=ATSCoverage(
        work_periods=Recoverable(total=4, recovered=3, lost=["Steppe Systems, 2019-2021"]),
        dates=Recoverable(total=8, recovered=7, lost=["с весны 2019"]),
        skills=Recoverable(total=20, recovered=20),
    ),
    source_format="pdf",
    page_count=2,
    word_count=612,
)

# ── the crawl ─────────────────────────────────────────────────────────

#: Runs, as ``pipeline_run`` records them: (id, slug, status, hours before the
#: epoch it started, found, new, updated, errors).
#:
#: The middle one is the case the overview screen has to show as its own
#: outcome. hh answered a permitted request with a check for robots, which is
#: not a broken connector and not a failed run: the pipeline records it under
#: the ``challenge`` stage and keeps the position, and the right reaction is to
#: come back later. Both live crawls of 2026-09-06 ended this way, so a seed
#: without it is a seed that never renders the normal case.
RUNS: tuple[tuple[UUID, str, PipelineRunStatus, int, int, int, int, list[dict[str, Any]]], ...] = (
    (
        UUID("0192f000-0000-7000-8000-00000000b001"),
        "hh",
        PipelineRunStatus.PARTIAL,
        3,
        172,
        41,
        131,
        [
            {
                "stage": "challenge",
                "error": "HHChallengedError",
                "detail": (
                    "almaty.hh.kz ответил проверкой на робота на 172-й странице; "
                    "позиция обхода сохранена, следующий прогон продолжит с неё"
                ),
            }
        ],
    ),
    (
        UUID("0192f000-0000-7000-8000-00000000b002"),
        "arbeitnow",
        PipelineRunStatus.SUCCESS,
        4,
        60,
        6,
        54,
        [],
    ),
    (
        UUID("0192f000-0000-7000-8000-00000000b003"),
        "jsearch",
        PipelineRunStatus.FAILED,
        30,
        0,
        0,
        0,
        [{"stage": "crawl", "error": "SourceError", "detail": "JSEARCH_API_KEY не задан"}],
    ),
)

#: Where the hh walk got to, per host and per sitemap file, written in exactly
#: the shape the connector writes: a position (finished stretches) and a census
#: (how big the file was and how much of it was still due). Two files on the
#: default host, one on the second, and one of them with two stretches — which
#: is what an interrupted run leaves behind and the only case where "how far did
#: it get" cannot be answered with a single date.
CRAWL_FILES: tuple[tuple[str, str, int, int, int], ...] = (
    ("almaty.hh.kz", "vacancy0", 1, 7_400, 6_157),
    ("almaty.hh.kz", "vacancy1", 2, 6_157, 5_980),
    ("astana.hh.kz", "vacancy0", 1, 4_120, 4_120),
)


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """Row counts the seed guarantees are in the database once it returns."""

    profiles: int
    profile_skills: int
    vacancies: int
    vacancy_sources: int
    matches: int
    applications: int


# ── deterministic builders ────────────────────────────────────────────


def _vacancy_seed(index: int) -> str:
    """Stable name for one seeded posting; every other key derives from it."""
    return f"dev-{index:03d}"


def _fingerprint(index: int) -> str:
    """The deduplication key, same 40-character shape the normaliser emits."""
    return hashlib.sha1(_vacancy_seed(index).encode()).hexdigest()


#: Deduplication key of every posting this seed owns, in seed order. Published
#: so a caller — the tests, a cleanup script — can tell the seeded rows apart
#: from anything a real connector put in the same table.
FINGERPRINTS: tuple[str, ...] = tuple(_fingerprint(index) for index in range(VACANCY_COUNT))


def _primary_slug(index: int) -> str:
    """Source that always carries this posting."""
    return SOURCES[index % len(SOURCES)]


def _mirror_slug(index: int) -> str:
    """Second source for a cross-posted posting; never the primary one."""
    return SOURCES[(index + 1) % len(SOURCES)]


def _source_row(index: int, slug: str) -> tuple[str, str, str, dict[str, Any]]:
    """The ``(slug, external_id, url, raw)`` tuple ``bulk_upsert`` expects."""
    name = _vacancy_seed(index)
    return (
        slug,
        f"{slug}-{name}",
        f"{SEED_URL_PREFIX}{slug}/{name}",
        {"seed": name, "source": slug},
    )


def _has_salary(index: int) -> bool:
    """False for the postings that deliberately advertise no money."""
    return index % NO_SALARY_MODULUS != NO_SALARY_REMAINDER


def _build_vacancy(index: int) -> VacancyCreate:
    """One normalised posting, fully determined by its index."""
    city, country = CITIES[index % len(CITIES)]
    remote = REMOTE_CYCLE[index % len(REMOTE_CYCLE)]

    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    currency: str | None = None
    period: SalaryPeriod | None = None
    if _has_salary(index):
        currency = CURRENCIES[index % len(CURRENCIES)]
        period = (
            SalaryPeriod.YEAR if index % YEARLY_MODULUS == YEARLY_REMAINDER else SalaryPeriod.MONTH
        )
        band_min, band_max = SALARY_BANDS[currency]
        # A spread of 1.00..1.30 so sorting by salary has something to order.
        spread = Decimal(1) + Decimal(index % 7) / Decimal(20)
        per_year = Decimal(12) if period is SalaryPeriod.YEAR else Decimal(1)
        salary_min = (band_min * spread * per_year).quantize(CENTS)
        salary_max = (band_max * spread * per_year).quantize(CENTS)

    published_at: datetime | None = None
    if index % NO_PUBLISHED_AT_MODULUS != NO_PUBLISHED_AT_REMAINDER:
        published_at = SEED_EPOCH - timedelta(days=index)

    title = TITLES[index % len(TITLES)]
    company = COMPANIES[index % len(COMPANIES)]
    return VacancyCreate(
        fingerprint=_fingerprint(index),
        title=title,
        company=company,
        company_url=f"https://example.test/company/{index % len(COMPANIES)}",
        description_raw=(
            f"{company} ищет {title}. Стек: Python, FastAPI, PostgreSQL, Docker. "
            "Плюсом будет опыт эксплуатации Kubernetes и очередей задач."
        ),
        seniority=SENIORITIES[index % len(SENIORITIES)],
        min_years=Decimal(f"{1 + index % 6}.0"),
        city=city,
        country=country,
        remote=remote,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=currency,
        is_gross=index % 2 == 0 if currency is not None else None,
        period=period,
        employment_type=(EmploymentType.CONTRACT if index % 10 == 7 else EmploymentType.FULL_TIME),
        language="ru" if country == "KZ" else "en",
        published_at=published_at,
    )


def _upsert_items() -> list[tuple[VacancyCreate, str, str, str, dict[str, Any]]]:
    """Every posting plus every place it was seen.

    Longer than :data:`VACANCY_COUNT`: the cross-posted indexes contribute a
    second source row against the same fingerprint, which is exactly how one
    vacancy ends up with two sources.
    """
    items: list[tuple[VacancyCreate, str, str, str, dict[str, Any]]] = []
    for index in range(VACANCY_COUNT):
        vacancy = _build_vacancy(index)
        items.append((vacancy, *_source_row(index, _primary_slug(index))))
        if index in CROSS_POSTED_INDEXES:
            items.append((vacancy, *_source_row(index, _mirror_slug(index))))
    return items


def _to_monthly_usd(amount: Decimal, currency: str, period: SalaryPeriod) -> Decimal | None:
    """Comparable monthly USD amount, or None when there is no known rate."""
    rate = DEV_USD_RATES.get(currency)
    if rate is None:
        return None
    return (amount * rate * PERIOD_TO_MONTH[period]).quantize(CENTS)


def _normalize_salary(vacancy: Vacancy) -> None:
    """Fill the monthly-USD columns the dashboard sorts on.

    Rewritten from scratch on every run rather than patched, so re-seeding
    after the rate table changes cannot leave a stale amount behind.
    """
    if vacancy.salary_min is None or vacancy.currency is None or vacancy.period is None:
        vacancy.salary_min_normalized = None
        vacancy.salary_max_normalized = None
        vacancy.salary_normalized_at = None
        return

    vacancy.salary_min_normalized = _to_monthly_usd(
        vacancy.salary_min, vacancy.currency, vacancy.period
    )
    vacancy.salary_max_normalized = (
        None
        if vacancy.salary_max is None
        else _to_monthly_usd(vacancy.salary_max, vacancy.currency, vacancy.period)
    )
    vacancy.salary_normalized_at = SEED_EPOCH


def _bucket_assignments() -> list[tuple[MatchBucket, Decimal]]:
    """One (bucket, score) pair per vacancy, covering all five buckets."""
    plan: list[tuple[MatchBucket, Decimal]] = []
    for bucket, count, base in BUCKET_PLAN:
        for offset in range(count):
            score = (Decimal(base) - Decimal(offset % 5) / Decimal(2)).quantize(CENTS)
            plan.append((bucket, score))
    if len(plan) != VACANCY_COUNT:
        raise RuntimeError(f"BUCKET_PLAN covers {len(plan)} vacancies, expected {VACANCY_COUNT}")
    return plan


def _build_match(
    *,
    profile_id: UUID,
    vacancy_id: UUID,
    index: int,
    bucket: MatchBucket,
    score: Decimal,
) -> MatchCreate:
    """A scoring result complete enough for the explanation panel."""
    matched = PROFILE_SKILLS[: 4 + index % 4]
    missing = MISSING_POOL[: MISSING_BY_BUCKET[bucket]]
    semantic = (score * Decimal("0.9")).quantize(CENTS)
    return MatchCreate(
        profile_id=profile_id,
        vacancy_id=vacancy_id,
        score=score,
        rule_score=score,
        semantic_score=semantic,
        bucket=bucket,
        component_scores=MatchComponentScores(
            skill_coverage_required=score,
            skill_coverage_nice=(score * Decimal("0.8")).quantize(CENTS),
            semantic_similarity=semantic,
            experience_fit=Decimal("80.00"),
            domain_fit=Decimal("70.00"),
            logistics_fit=Decimal("90.00"),
        ),
        matched_skills=[
            MatchedSkill(canonical_name=name, coverage=Decimal("1.0")) for name in matched
        ],
        missing_required=[
            MissingSkill(canonical_name=name, weight=Decimal("1.0")) for name in missing
        ],
        experience_gap_years=Decimal(f"-{index % 3}.0"),
        verdict=VERDICT_BY_BUCKET[bucket],
        application_angle="Сделать акцент на асинхронных пайплайнах и нагрузке.",
    )


def _profile_payload() -> CandidateProfileCreate:
    """The single candidate the dashboard scores everything against."""
    return CandidateProfileCreate(
        name=PROFILE_NAME,
        headline="Senior Backend Engineer, Python / async",
        seniority=Seniority.SENIOR,
        total_years=Decimal("7.0"),
        summary="Бэкенд на Python: асинхронные сервисы, интеграции, данные.",
        locations=["Алматы", "Астана"],
        relocation=True,
        remote_pref=RemoteType.FULL,
        salary_min=Decimal("4500.00"),
        salary_currency="USD",
        languages=[{"code": "ru", "level": "native"}, {"code": "en", "level": "B2"}],
        raw_text=RESUME_TEXT,
        skills=[
            SkillCreate(
                canonical_name=name,
                # ``raw_names``, plural, and it used to be ``raw_name`` — a key
                # ``SkillCreate`` does not declare and Pydantic therefore
                # dropped, so every seeded skill reached the database with an
                # empty spelling list and the vacancy card rendered "postgresql"
                # where the resume says "PostgreSQL".
                raw_names=[name.title()],
                years=Decimal(f"{2 + position % 5}.0"),
                level=SkillLevel.STRONG if position < 10 else SkillLevel.WORKING,
                last_used_year=2026 - position % 3,
            )
            for position, name in enumerate(PROFILE_SKILLS)
        ],
    )


# ── the seed itself ───────────────────────────────────────────────────


async def _seed_profile(session: AsyncSession) -> CandidateProfile:
    """Insert the candidate once; later runs reuse the same row."""
    repository = ProfileRepository(session)
    existing = await repository.get(SEED_PROFILE_ID)
    if existing is not None:
        return existing

    payload = _profile_payload()
    # ProfileRepository.create() allocates its own uuid7, and the seed needs a
    # stable id so re-running it recognises its own row instead of adding a
    # second active profile. Same construction, fixed primary key.
    profile = CandidateProfile(id=SEED_PROFILE_ID, **payload.model_dump(exclude={"skills"}))
    profile.skills = [ProfileSkill(**skill.model_dump()) for skill in payload.skills]
    # The upload facts and the audit taken with them. Without these the
    # documents screen has a resume row with no file, no verdict and no
    # findings, which is the one state a real upload never produces.
    profile.parse_status = ParseStatus.READY
    profile.resume_filename = "resume-2026.pdf"
    profile.resume_format = "pdf"
    profile.resume_size_bytes = 212_992
    profile.ats_report = ATS_REPORT.model_dump(mode="json")
    session.add(profile)
    await session.flush()
    return profile


async def _seed_vacancies(session: AsyncSession) -> list[UUID]:
    """Upsert every posting and return the ids in seed-index order."""
    repository = VacancyRepository(session)
    await repository.bulk_upsert(_upsert_items())

    # bulk_upsert returns ids in the order the database handed them back, which
    # is not the seed order the matches and applications index into. Reading
    # them back by fingerprint is the only ordering that is actually stable.
    vacancy_ids: list[UUID] = []
    for index in range(VACANCY_COUNT):
        vacancy = await repository.get_by_fingerprint(_fingerprint(index))
        if vacancy is None:
            raise RuntimeError(f"vacancy {_vacancy_seed(index)} vanished right after its upsert")
        _normalize_salary(vacancy)
        vacancy_ids.append(vacancy.id)

    await session.flush()
    return vacancy_ids


async def _seed_matches(
    session: AsyncSession, profile_id: UUID, vacancy_ids: Sequence[UUID]
) -> int:
    """Score every posting, spreading the results over all five buckets."""
    repository = MatchRepository(session)
    matches = [
        _build_match(
            profile_id=profile_id,
            vacancy_id=vacancy_ids[index],
            index=index,
            bucket=bucket,
            score=score,
        )
        for index, (bucket, score) in enumerate(_bucket_assignments())
    ]
    await repository.bulk_upsert(matches)
    return len(matches)


async def _seed_applications(
    session: AsyncSession, profile_id: UUID, vacancy_ids: Sequence[UUID]
) -> int:
    """Put a few postings into the tracker, in every state the board renders.

    ``sent_at`` and ``sent_letter`` are written only for the rows marked sent,
    and that restraint is the point rather than tidiness: those two columns are
    the evidence that an application actually went out, every counter on the
    overview screen reads them, and a seed that filled them in for a queued row
    would make the one number this project can verify unverifiable.
    """
    for seeded in APPLICATIONS:
        if await session.get(Application, seeded.id) is not None:
            continue
        sent_at = SEED_EPOCH - timedelta(days=seeded.days_ago or 0) if seeded.sent else None
        session.add(
            Application(
                id=seeded.id,
                vacancy_id=vacancy_ids[seeded.vacancy_index],
                profile_id=profile_id if seeded.letter is not None else None,
                status=seeded.status,
                applied_at=(
                    None
                    if seeded.days_ago is None
                    else SEED_EPOCH - timedelta(days=seeded.days_ago)
                ),
                notes=APPLICATION_NOTES[seeded.status],
                cover_letter=seeded.letter,
                letter_rules_version=seeded.rules_version,
                agent_status=seeded.agent_status,
                agent_reason=seeded.agent_reason,
                sent_at=sent_at,
                sent_letter=seeded.letter if seeded.sent else None,
                match_score=_score_of(seeded.vacancy_index) if seeded.sent else None,
                match_bucket=_bucket_of(seeded.vacancy_index) if seeded.sent else None,
                vacancy_key_skills=list(PROFILE_SKILLS[:4]) if seeded.sent else None,
                hh_warning=seeded.hh_warning,
                hh_blocking_warning=seeded.hh_blocking_warning,
                hh_negotiations_total=seeded.hh_negotiations_total,
                hh_last_state=seeded.hh_last_state,
                hh_last_state_at=(
                    None if seeded.hh_last_state is None else SEED_EPOCH - timedelta(days=2)
                ),
            )
        )
    await session.flush()
    return len(APPLICATIONS)


def _score_of(index: int) -> Decimal:
    """The score this vacancy carries, so a send's snapshot is not invented."""
    return _bucket_assignments()[index][1]


def _bucket_of(index: int) -> MatchBucket:
    """The bucket that went with it."""
    return _bucket_assignments()[index][0]


async def _seed_previous_profile(session: AsyncSession) -> None:
    """An older, inactive resume, claiming one skill the current one does not.

    Without it the vacancy card has no way to show its middle case — a
    requirement the score counts as missing that the candidate demonstrably
    has — because the only evidence for that case which is not the CV being
    scored is another CV. Inactive, so nothing else in the project reads it: the
    scorer, the queue and the letters all work off the active profile.
    """
    if await session.get(CandidateProfile, SEED_PREVIOUS_PROFILE_ID) is not None:
        return
    profile = CandidateProfile(
        id=SEED_PREVIOUS_PROFILE_ID,
        name=PROFILE_NAME,
        headline="Backend Engineer, Python / data",
        seniority=Seniority.MIDDLE,
        total_years=Decimal("5.0"),
        summary="Предыдущая версия резюме, оставлена для истории.",
        locations=["Алматы"],
        raw_text="Older seeded resume. Kept so the current one can be compared with it.",
        is_active=False,
        parse_status=ParseStatus.READY,
        resume_filename="resume-2024.pdf",
        resume_format="pdf",
        resume_size_bytes=184_320,
    )
    profile.skills = [
        ProfileSkill(canonical_name=name, raw_names=[name.title()], level=SkillLevel.WORKING)
        for name in PREVIOUS_PROFILE_SKILLS
    ]
    session.add(profile)
    await session.flush()


async def _seed_runs(session: AsyncSession) -> int:
    """Recent runs, including the one the overview shows as its own outcome."""
    written = 0
    for run_id, slug, status, hours_ago, found, new, updated, errors in RUNS:
        if await session.get(PipelineRun, run_id) is not None:
            continue
        started = SEED_EPOCH - timedelta(hours=hours_ago)
        session.add(
            PipelineRun(
                id=run_id,
                source_slug=slug,
                status=status,
                started_at=started,
                finished_at=started + timedelta(minutes=19),
                found=found,
                new=new,
                updated=updated,
                errors=errors,
            )
        )
        written += 1
    await session.flush()
    return written


async def _seed_crawl_position(session: AsyncSession) -> int:
    """Where the hh walk got to, written the way the connector writes it.

    Built out of the connector's own models rather than hand-rolled JSON, so a
    change to either shape breaks the seed here instead of producing a crawl
    panel that renders nothing and says nothing about why.
    """
    states = SourceStateRepository(session)
    written = 0
    for host, name, stretches, total, outstanding in CRAWL_FILES:
        site = HHSite(host=host, city=host.split(".")[0], country="KZ")
        covered = tuple(
            Span(
                low_lastmod=SEED_EPOCH - timedelta(days=3 + step * 4),
                low_id=f"{100_000 + step * 10}",
                high_lastmod=SEED_EPOCH - timedelta(days=1 + step * 4),
                high_id=f"{100_009 + step * 10}",
            )
            for step in range(stretches)
        )
        await states.set(
            "hh",
            f"{POSITION_PREFIX}{site.host}:{name}",
            FileWatermark(covered=covered).model_dump(mode="json"),
        )
        await states.set(
            "hh",
            f"{CENSUS_PREFIX}{site.host}:{name}",
            FileCensus(total=total, outstanding=outstanding).model_dump(mode="json"),
        )
        written += 2
    await session.flush()
    return written


async def seed(session: AsyncSession) -> SeedSummary:
    """Write the whole development data set through the given session.

    The caller owns the transaction: this flushes but never commits, so a test
    can run it inside a transaction it rolls back afterwards.
    """
    profile = await _seed_profile(session)
    await _seed_previous_profile(session)
    vacancy_ids = await _seed_vacancies(session)
    match_count = await _seed_matches(session, profile.id, vacancy_ids)
    application_count = await _seed_applications(session, profile.id, vacancy_ids)
    await _seed_runs(session)
    await _seed_crawl_position(session)

    return SeedSummary(
        # Two resumes, not one. The older one is inactive and nothing else in
        # the project reads it; it exists so the vacancy card can show a
        # requirement the candidate holds and this CV does not name, which is
        # the one case that cannot be seeded from a single profile.
        profiles=2,
        profile_skills=len(PROFILE_SKILLS) + len(PREVIOUS_PROFILE_SKILLS),
        vacancies=len(vacancy_ids),
        vacancy_sources=len(_upsert_items()),
        matches=match_count,
        applications=application_count,
    )


async def main() -> None:
    """Seed the database named by ``settings.database_url``."""
    configure_logging()
    engine = create_async_engine(settings.database_url, future=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            summary = await seed(session)
            await session.commit()
    finally:
        await engine.dispose()

    logger.info("seed.finished", **asdict(summary))


if __name__ == "__main__":
    asyncio.run(main())
