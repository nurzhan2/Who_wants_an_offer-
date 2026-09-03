# Фаза 1 (ревизия). Слой данных

Заменяет промт фазы 1 из `prompts/01-foundation.md`. Переработан по факту
того, что собралось в фазе 0: `filterwarnings = error`, локальный
PostgreSQL 17 на `:5432`, `pgvector` только в Docker, Windows-окружение.

---

## Преконтроль (сделать до промта)

```bash
# docker-compose.yml: образ → pgvector/pgvector:pg17, порт → "5433:5432"
# .env: DATABASE_URL и TEST_DATABASE_URL → порт 5433
docker compose up -d db
docker compose exec db psql -U postgres -d offers \
  -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname='vector';"
```

Не отдаёт версию — фазу не начинать.

---

## Промт

```
Фаза 1. Прочитай CLAUDE.md и docs/ARCHITECTURE.md, раздел «Схема БД» —
реализуй её. Фаза 0 закрыта: конфиг, логирование, обработка ошибок,
сессия БД и CI работают. База — PostgreSQL 17 + pgvector в Docker на
порту 5433.

ЗАДАЧА: слой данных, миграции, репозитории.

ЧЕТЫРЕ МЕСТА, ГДЕ ТЫ НАСТУПИШЬ, ЕСЛИ НЕ ПРЕДУПРЕДИТЬ — прочитай до того,
как начнёшь план:

  (а) Native enum в PostgreSQL. Alembic autogenerate НЕ создаёт и НЕ
      удаляет типы enum — он молча генерирует миграцию, которая падает на
      чистой базе. В первой миграции типы создаются явно через op.execute
      ДО create_table, в downgrade — DROP TYPE после drop_table. В моделях
      sa.Enum(..., name="...", create_type=False). Проверяется тестом
      upgrade head → downgrade base → upgrade head на чистой БД.

  (б) Размерность вектора. Vector(1024) в миграции — литерал, а
      settings.EMBEDDING_DIM — конфиг. Они разъедутся, и ты узнаешь об этом
      на первом INSERT эмбеддинга через полтора месяца. Добавь проверку при
      старте приложения: читаем фактическую размерность колонки из
      information_schema / pg_attribute, сравниваем с settings.EMBEDDING_DIM,
      при расхождении — падаем на старте с внятным сообщением.

  (в) filterwarnings = error уже включён. Любой DeprecationWarning из
      SQLAlchemy 2.0 или Alembic уронит тесты. Пиши сразу в стиле 2.0:
      Mapped[]/mapped_column, select() вместо Query, никаких legacy-API.

  (г) Полнотекстовый индекс. Вакансии смешанные ru/en, а конфигурация
      to_tsvector должна быть IMMUTABLE — выбрать её по языку строки в
      generated column нельзя. Используй конфигурацию 'simple' (без
      стемминга) для generated-колонки search_vector, GIN по ней. Стемминг
      по языку, если понадобится, добавим отдельно позже. Не пытайся
      сделать per-row конфигурацию — миграция не применится.

ЧТО ДЕЛАТЬ:

1. app/db/base.py — DeclarativeBase + миксины:
   - id: UUID primary key. Используй UUIDv7 (пакет uuid-utils), не uuid4 —
     монотонный по времени идентификатор даёт локальность в B-tree и
     стабильный tiebreaker для keyset-пагинации. Если пакет не встаёт на
     Windows — uuid4, но зафиксируй причину в докстринге.
   - created_at / updated_at: DateTime(timezone=True), server_default
     func.now(), updated_at с onupdate
   - naming_convention для constraint'ов в MetaData — иначе Alembic будет
     генерировать безымянные ограничения, и downgrade сломается

2. app/db/models.py — модели из docs/ARCHITECTURE.md:
   CandidateProfile, ProfileSkill, Vacancy, VacancySource, VacancySkill,
   Match, Application, PipelineRun.

   Типы — внимательно:
   - деньги: Numeric(12, 2), не Float. Никаких float для зарплат
   - произвольные структуры: JSONB, не JSON
   - embedding: Vector(settings.EMBEDDING_DIM), nullable
   - скоры: Numeric(5, 2) или Float — но зафиксируй выбор и не смешивай
   - все enum'ы питоновские, в БД native типы (см. пункт «а»)

   Связи двусторонние, lazy="selectin" для коллекций, которые всегда
   нужны вместе с родителем (ProfileSkill, VacancySource, VacancySkill),
   lazy="raise" для тех, что не должны подгружаться неявно — лучше
   явная ошибка, чем N+1 в проде.

   cascade="all, delete-orphan" на дочерних.

   Уникальные ограничения: vacancy.fingerprint;
   (vacancy_source.source_slug, external_id);
   (match.profile_id, match.vacancy_id);
   (profile_skill.profile_id, canonical_name);
   (vacancy_skill.vacancy_id, canonical_name).

3. app/schemas/ — Pydantic v2, разделённые Create / Update / Read.
   Read с from_attributes=True. Ключевые:
   - VacancyListItem — облегчённая для таблицы, ровно те поля, что нужны
     фронту: id, title, company, source_slugs, city, remote, salary,
     score, bucket, missing_required_count, published_at, is_applied
   - VacancyRead — полная карточка
   - MatchDetail — с component_scores и всеми списками скиллов
   - VacancyFilter — все фильтры из docs/ARCHITECTURE.md одним объектом,
     с валидаторами: score_min <= score_max, salary_min >= 0,
     posted_within_days в разумных границах, sort из допустимого набора
   - CursorPage[T] — generic-обёртка ответа: items, next_cursor, total,
     facets

4. Alembic:
   - async env.py, читает DATABASE_URL из settings, compare_type=True,
     compare_server_default=True
   - render_as_batch не нужен (не SQLite)
   - первая миграция: CREATE EXTENSION IF NOT EXISTS vector →
     CREATE TYPE для всех enum → таблицы → индексы
   - индексы:
     * HNSW по vacancy.embedding, vector_cosine_ops, m=16,
       ef_construction=64
     * HNSW по candidate_profile.embedding
     * GIN по vacancy.search_vector (generated column, конфигурация
       'simple')
     * btree vacancy(published_at DESC), частичный WHERE is_active
     * btree match(profile_id, score DESC)
     * btree vacancy_source(source_slug, external_id) — покрыт unique
   - downgrade полный и рабочий, включая DROP TYPE и DROP EXTENSION

5. app/db/repositories/ — типизированные репозитории, сырых запросов в
   сервисах быть не должно:

   - VacancyRepository:
     * upsert_by_external_id — через postgresql.insert().
       on_conflict_do_update, RETURNING, один запрос, идемпотентно
     * bulk_upsert — батчем, не циклом
     * get_by_fingerprint
     * list_filtered(filter, cursor, limit) — keyset-пагинация:
       курсор кодирует (значение сортировки, id) в base64, WHERE
       (sort_col, id) < (:val, :id) с составным сравнением. Offset не
       использовать. Tiebreaker по id обязателен, иначе при равных
       скорах записи будут пропадать между страницами
     * facets(filter) — счётчики по source / bucket / city одним запросом
       с FILTER, не пятью

   - ProfileRepository, MatchRepository (bulk_upsert,
     top_for_profile(limit)), PipelineRunRepository.

6. Проверка размерности вектора при старте — см. пункт «б». Добавь в
   lifespan, после проверки соединения.

7. scripts/seed.py — данные для разработки фронта:
   1 профиль с 20 скиллами, 60 вакансий из 4 источников, разброс скоров
   по всем бакетам, 5 вакансий-дублей (одна Vacancy, несколько
   VacancySource) чтобы фронт сразу видел этот кейс, 3 отклика в разных
   статусах. Идемпотентен: повторный запуск не плодит.

8. Тесты:
   - upgrade head → downgrade base → upgrade head на чистой БД проходит
     (это тест на пункт «а», не пропускай его)
   - каскадное удаление профиля убирает скиллы и матчи
   - upsert идемпотентен: два вызова с теми же данными → одна строка,
     updated_at обновился
   - keyset-пагинация: 200 записей, проходим все страницы, вставляем
     запись между страницами — нет ни пропусков, ни дублей; отдельный
     кейс с 50 записями с одинаковым скором
   - каждый фильтр VacancyFilter параметризованно, включая границы
   - enum сохраняется и читается корректно
   - при расхождении EMBEDDING_DIM с колонкой приложение падает на старте
   - Numeric не теряет копейки на round-trip

ЧЕГО НЕ ДЕЛАТЬ:
   - create_all вне тестовых фикстур
   - синхронные сессии
   - модель telegram_channel_state — она в фазе 3b, не здесь
   - бизнес-логику в репозиториях: они знают про SQL, не про скоринг

DoD из CLAUDE.md. Прогони ruff, mypy, pytest, покажи вывод, плюс отдельно
покажи вывод upgrade/downgrade/upgrade на чистой базе.

Сначала план, потом код.
```

---

## На что смотреть в ревью

1. `alembic downgrade base && alembic upgrade head` действительно
   отрабатывает — это единственная реальная проверка миграции.
2. В `list_filtered` нет `OFFSET`. Если появился — keyset не сделан.
3. `facets` — один запрос с `FILTER`, а не пачка `count()`.
4. Ни одного `Float` рядом с зарплатой.
5. Тест пагинации со вставкой между страницами есть и падает, если
   убрать tiebreaker по id.
