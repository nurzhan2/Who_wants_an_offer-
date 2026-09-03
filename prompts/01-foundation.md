# Фазы 0–1. Фундамент

---

## Промт фазы 0 — каркас репозитория и тулинг

```
Ты работаешь в репозитории Who_wants_an_offer- — агрегатор вакансий,
управляемый резюме. Сначала прочитай CLAUDE.md, README.md,
docs/ARCHITECTURE.md, docs/SOURCES.md, docs/MATCHING.md, docs/ROADMAP.md.
Код пока не написан — есть только документация.

ЗАДАЧА ФАЗЫ 0: собрать production-ready каркас проекта. Код приложения
в этой фазе не пишем — только инфраструктура, тулинг и скелет.

Сделай:

1. pyproject.toml для backend (Python 3.12, менеджер uv):
   - зависимости: fastapi, uvicorn[standard], sqlalchemy[asyncio]>=2.0,
     asyncpg, alembic, pydantic>=2, pydantic-settings, httpx, tenacity,
     structlog, pgvector, python-multipart, anthropic
   - dev-группа: pytest, pytest-asyncio, pytest-cov, httpx, ruff, mypy,
     types-*, respx, pre-commit, faker
   - конфиг ruff (line-length 100, правила E,F,I,N,UP,B,SIM,RUF, isort)
   - конфиг mypy strict для app/
   - конфиг pytest: asyncio_mode=auto, покрытие

2. Структуру каталогов ровно как в docs/ARCHITECTURE.md, с __init__.py
   и заглушками-докстрингами. Пустых бессмысленных файлов не плоди.

3. backend/app/core/config.py — Settings на pydantic-settings:
   DATABASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, EMBEDDING_MODEL,
   LOG_LEVEL, ENVIRONMENT, HTTP_TIMEOUT, USER_AGENT, ADZUNA_APP_ID,
   ADZUNA_APP_KEY, JOOBLE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
   MATCH_SCORE_ALERT_THRESHOLD, LLM_RERANK_TOP_N.
   Все опциональные ключи источников — Optional с None по умолчанию.

4. backend/app/core/logging.py — structlog, JSON в проде, human-readable
   в dev, request_id в контексте.

5. backend/app/core/exceptions.py — базовые доменные исключения
   (SourceError, RateLimitError, ParsingError, LLMError) и обработчики
   для FastAPI, отдающие RFC 7807 problem+json.

6. backend/app/main.py — приложение FastAPI с lifespan, CORS,
   middleware request_id, роутером /health (проверяет БД) и подключением
   пустого роутера api/v1.

7. docker-compose.yml: сервис db (pgvector/pgvector:pg16, volume, healthcheck),
   сервис api (build из backend/Dockerfile, depends_on db healthy),
   сервис frontend для dev. Плюс backend/Dockerfile (multi-stage, non-root).

8. .env.example со всеми переменными и комментариями, .gitignore
   (python, node, .env, .venv, __pycache__, .ruff_cache, .mypy_cache,
   dist, .cache, *.db, uploads/).

9. .pre-commit-config.yaml: ruff, ruff-format, mypy, end-of-file-fixer,
   trailing-whitespace, check-yaml, detect-private-key.

10. .github/workflows/ci.yml: на push и PR — lint (ruff), typecheck (mypy),
    tests (pytest с сервисом postgres+pgvector), сборка фронта.
    Кэш uv и npm.

11. Makefile: install, dev, test, lint, fmt, typecheck, migrate, revision,
    up, down, seed.

12. tests/conftest.py с фикстурами: event_loop, async_engine на тестовой БД,
    db_session с откатом транзакции, async_client (httpx ASGITransport).
    Один smoke-тест на /health.

ТРЕБОВАНИЯ:
- Никаких секретов в коде и дефолтах.
- Всё типизировано, mypy strict проходит.
- После генерации сам прогони: uv sync, ruff check, mypy backend/app,
  pytest. Покажи вывод. Если что-то падает — чини, не оставляй красным.

ПОРЯДОК: сначала покажи мне план файлов, которые создашь, одним списком.
Дождись моего «go». Только потом пиши код.
```

---

## Промт фазы 1 — модели данных и миграции

```
Фаза 1. Фундамент из фазы 0 уже в репозитории. Прочитай CLAUDE.md и
docs/ARCHITECTURE.md, раздел «Схема БД» — реализуй её точно.

ЗАДАЧА: слой данных и миграции.

1. backend/app/db/base.py — DeclarativeBase с общими миксинами:
   id (UUID pk, default uuid4), created_at/updated_at (timezone-aware,
   server_default now()).

2. backend/app/db/models.py — SQLAlchemy 2.0 модели в стиле Mapped[]:
   CandidateProfile, ProfileSkill, Vacancy, VacancySource, VacancySkill,
   Match, Application, PipelineRun.
   - все enum'ы — питоновские Enum, в БД native enum типами
   - embedding — pgvector Vector(1024), nullable
   - relationships двусторонние, с lazy="selectin" где логично,
     cascade="all, delete-orphan" для дочерних
   - уникальные ограничения: vacancy.fingerprint,
     (vacancy_source.source_slug, external_id),
     (match.profile_id, match.vacancy_id),
     (profile_skill.profile_id, canonical_name)

3. backend/app/schemas/ — Pydantic v2 модели, разделённые на
   Create / Update / Read / Internal. Read-модели с from_attributes=True.
   Ключевые: CandidateProfileRead, SkillRead, VacancyRead, VacancyListItem
   (облегчённая, для таблицы), MatchDetail, PipelineRunRead,
   VacancyFilter (все фильтры из docs/ARCHITECTURE.md как один
   Query-объект с валидацией диапазонов).

4. backend/app/db/session.py — async engine, async_sessionmaker,
   зависимость get_session с корректным закрытием.

5. Alembic: инициализация под async, env.py читающий DATABASE_URL из
   settings, первая миграция со всей схемой. В миграции обязательно
   CREATE EXTENSION IF NOT EXISTS vector, HNSW-индекс по
   vacancy.embedding (vector_cosine_ops), GIN-индекс для полнотекста по
   description_raw, btree на vacancy.published_at и match(profile_id, score).

6. backend/app/db/repositories/ — репозитории с типизированными методами
   вместо голых запросов в сервисах: VacancyRepository (upsert_by_external_id,
   list_filtered с пагинацией и сортировкой, get_by_fingerprint),
   ProfileRepository, MatchRepository (bulk_upsert, top_for_profile),
   PipelineRunRepository.

7. Тесты: миграция применяется на чистой БД и откатывается; каскадные
   удаления работают; upsert идемпотентен; list_filtered корректно
   применяет каждый фильтр (параметризованный тест).

8. scripts/seed.py — наполнение тестовыми данными для разработки фронта:
   1 профиль, 60 вакансий из 4 источников с разбросом скоров.

ТРЕБОВАНИЯ:
- Никакого create_all вне тестов.
- Все запросы асинхронные, никаких sync-сессий.
- mypy strict, ruff, pytest — зелёные. Прогони и покажи вывод.

Сначала план, потом код.
```
