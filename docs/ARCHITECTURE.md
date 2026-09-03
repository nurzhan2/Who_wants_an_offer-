# Архитектура

## Поток данных

```
CV (pdf/docx/txt)
   │
   ├─► extract text (pdfplumber / python-docx / OCR fallback)
   │
   ├─► LLM extraction ──► CandidateProfile (структура + скиллы + годы)
   │                             │
   │                             └─► embedding (bge-m3) ──► pgvector
   │
   └─► search plan generation (queries × areas × sources)
                 │
                 ▼
       ┌──────────────────────┐
       │   Source Registry    │  hh, adzuna, jooble, greenhouse, lever,
       │  (плагины BaseSource)│  remotive, arbeitnow, himalayas, habr, tg…
       └──────────┬───────────┘
                  │ RawPosting
                  ▼
        Normalizer ──► skill extraction ──► Vacancy (единая схема)
                  │
                  ▼
        Deduplicator (fingerprint + simhash) ──► upsert в БД
                  │
                  ▼
        Matcher: hard filters → rule score → semantic → LLM re-rank (top-N)
                  │
                  ▼
        MatchResult (score, matched[], missing[], gaps, verdict)
                  │
                  ├─► REST API ──► React Dashboard
                  └─► Telegram alert (score ≥ порога)
```

## Модули

### `resume/`
- `extractor.py` — текст из файла. PDF: `pdfplumber`, фолбэк `pypdf`; скан →
  OCR (`pytesseract`, ru+eng+kaz). DOCX: `python-docx`.
- `profile_builder.py` — LLM-экстракция в `CandidateProfile`. Строгая
  Pydantic-схема, один ретрай при невалидном JSON.
- `enricher.py` — нормализация скиллов по словарю, расчёт `years_per_skill`
  из дат работы, вывод seniority.

### `sources/`
`base.py`:
```python
class BaseSource(ABC):
    slug: str
    regions: list[str]
    requires_auth: bool
    rate_limit: RateLimit

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]: ...
    async def fetch_detail(self, posting: RawPosting) -> RawPosting: ...
```
Регистрация через декоратор `@register_source`. Пайплайн знает только реестр.

`SearchQuery`: `keywords[]`, `area`, `remote`, `salary_min`, `posted_within_days`,
`employment_type`, `language`.

`RawPosting`: сырые поля источника + `source_slug`, `external_id`, `url`,
`fetched_at`, `raw: dict` (для отладки и повторной нормализации без перепарсинга).

### `normalize/`
- `skills.yaml` — канонический словарь: `postgresql: [постгрес, postgres, pg,
  психоаналитик-нет]`, группы (`backend`, `devops`), связанные скиллы с весом
  близости (`django ↔ fastapi = 0.6`).
- `mapper.py` — `RawPosting → Vacancy`, парсинг зарплат (разные валюты,
  gross/net, «от/до», «по договорённости»), парсинг требуемого опыта,
  определение seniority и языка вакансии.
- `dedup.py` — fingerprint `sha1(norm_company | norm_title | city)` +
  `simhash(description)`; при совпадении объединяет записи, хранит все
  источники и ссылки в `vacancy_sources`.

### `matching/`
См. `docs/MATCHING.md`.

### `pipeline/`
- `runner.py` — прогон плана поиска по всем включённым источникам,
  параллельно с семафором на источник, отчёт `PipelineRun` в БД.
- `scheduler.py` — APScheduler: полный прогон раз в N часов, инкрементальный
  (только новое за 24ч) чаще.

## Схема БД

```
candidate_profile
  id, name, headline, seniority, total_years, summary,
  locations jsonb, relocation bool, remote_pref,
  salary_min, salary_currency, languages jsonb,
  raw_text, embedding vector(1024), created_at, is_active

profile_skill
  id, profile_id → candidate_profile, canonical_name, raw_name,
  years float, level enum(basic|working|strong|expert), last_used_year

vacancy
  id, fingerprint unique, title, company, company_url,
  description_raw, description_md, seniority, min_years,
  city, country, remote enum(no|hybrid|full),
  salary_min, salary_max, currency, is_gross, period,
  employment_type, language, published_at, expires_at,
  embedding vector(1024), first_seen_at, last_seen_at, is_active

vacancy_source
  id, vacancy_id → vacancy, source_slug, external_id, url, raw jsonb
  unique(source_slug, external_id)

vacancy_skill
  id, vacancy_id, canonical_name, is_required bool, weight float

match
  id, profile_id, vacancy_id, score float, rule_score, semantic_score,
  llm_score, bucket, matched_skills jsonb, missing_required jsonb,
  missing_nice jsonb, experience_gap_years float, verdict text,
  red_flags jsonb, scored_at
  unique(profile_id, vacancy_id)

application
  id, vacancy_id, status enum(saved|applied|screening|interview|offer|rejected),
  applied_at, notes, cover_letter, updated_at

pipeline_run
  id, started_at, finished_at, source_slug, status,
  found, new, updated, errors jsonb
```

Индексы: `vacancy.embedding` — HNSW cosine; `vacancy(published_at desc)`;
`match(profile_id, score desc)`; GIN на `vacancy.description_raw` для FTS.

## API (v1)

```
POST   /api/v1/resume/upload          multipart → profile_id, задача разбора
GET    /api/v1/profile/{id}
PATCH  /api/v1/profile/{id}           ручная правка скиллов/предпочтений
POST   /api/v1/profile/{id}/rescore   пересчёт матчей

GET    /api/v1/vacancies              фильтры + пагинация + сортировка
GET    /api/v1/vacancies/{id}
GET    /api/v1/vacancies/{id}/match   полный разбор соответствия
POST   /api/v1/vacancies/{id}/cover-letter

GET    /api/v1/sources                список, статус, последний прогон
POST   /api/v1/pipeline/run           ручной запуск, опционально по источникам
GET    /api/v1/pipeline/runs

GET    /api/v1/analytics/skill-gaps   топ недостающих скиллов по рынку
GET    /api/v1/analytics/salary       распределение по совпадающим вакансиям

GET/POST/PATCH /api/v1/applications
```

Фильтры `/vacancies`: `score_min`, `score_max`, `bucket`, `source`, `remote`,
`city`, `country`, `salary_min`, `currency`, `seniority`, `posted_within_days`,
`has_salary`, `missing_skills_max`, `company`, `q` (полнотекст),
`exclude_applied`, `sort` (`score|published_at|salary`).
