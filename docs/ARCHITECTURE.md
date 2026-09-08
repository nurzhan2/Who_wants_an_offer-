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
- `ats_audit.py` — машиночитаемость резюме для ATS работодателя. См. ниже.

### ATS-аудит

Мы отдаём PDF модели напрямую, и она видит страницу. Applicant tracking
system работодателя так не умеет: она читает текстовый слой и больше ничего.
Резюме поэтому может отлично работать здесь и быть невидимым везде, куда его
отправляют, — а человек не узнает, почему ему не звонили.

Аудит моделирует именно этот примитивный парсер. Без LLM: ответ обязан
приходить из того же текстового слоя, который читает ATS.

Отчёт складывается из двух половин, готовых в разные моменты.

**Структурная** читает сам файл — геометрию колонок, глифы, контакты,
заголовки, таблицы — и готова в момент загрузки, до всякой модели. Поэтому
считается в запросе `POST /resume/upload`, а не в фоновой задаче: файл
удаляется, как только разбор закончился, и отложенный аудит стало бы нечем
пересчитать. Плюс «твоё резюме не прочитает ни один робот» — единственное
полезное, что можно сказать человеку за первую секунду, а не через сорок.

**Покрытие** — то, ради чего всё затевалось: структура, прочитанная моделью
(она видела вёрстку и права), против структуры, восстановимой из одного
текстового слоя (это всё, что получит работодатель). Разница и есть цифра для
пользователя: «ATS увидит 3 места работы из 4». Считается, когда разбор
закончился, и переписывает отчёт целиком. Дополнительного вызова модели не
делает — переиспользует уже оплаченную экстракцию.

Строка считается прочитанной, только если парсер отнесёт её куда надо:
строки, склеенные из двух колонок, и строки с заголовком секции не в начале
выбрасываются. Слова в них есть — но работодатель прочтёт фриланс-подработку
как часть списка навыков, а это хуже, чем не прочитать вовсе.

`overall` (`ok` / `degraded` / `unreadable`) выводится из находок, а не
хранится: сохранённый вердикт был бы единственной частью отчёта, которую
ничто не проверяет. `coverage = null` означает «не измеряли», а не «потерь
нет» — иначе каждое резюме выглядело бы идеальным те полминуты, пока идёт
разбор.

Отчёт лежит в `candidate_profile.ats_report` (JSONB), контракт —
`app/schemas/ats.py`. Пороги — в конфиге (`ATS_*`), потому что правильное
значение зависит от резюме, которые реально загружают.

### `sources/`
- `base.py` — контракт: `SearchQuery`, `RawPosting`, `RateLimit`, `BaseSource`.
- `registry.py` — `@register_source`, ленивый обход пакета, `get_enabled_sources()`.
- `http.py` — общий клиент: токен-бакет, ретраи, robots, дисковый кэш.
- `query_planner.py` — профиль → небольшой набор запросов. См. ниже.
- `jsearch.py`, `arbeitnow.py`, `remotive.py` — коннекторы.

Добавление источника — один файл в этом пакете. Ничего снаружи не меняется:
реестр наполняется обходом пакета при первом обращении, а не в `lifespan`
(в тестах он не выполняется) и не в `__init__.py` (это цикл импорта: коннектор
импортирует `base`, что запускает `__init__`, который импортирует коннектор).
Включение источников — три источнико-агностичных настройки, добавленные один
раз: `SOURCES_DISABLED`, `SOURCES_ENABLED`, `SOURCE_CREDENTIALS`.

### robots.txt и `access_mode`

`CRAWL` (по умолчанию) читает robots. `API` — не читает, потому что robots
адресован краулерам, обходящим страницы, а не клиенту документированного API
по контракту: буквальное применение отключило бы `jsearch`, у которого на
шлюзовом хосте стоит `Disallow: /`, хотя весь хост — платные эндпоинты с
ключом. Послабление платное: реестр **отказывается регистрировать** `API`-класс
без `terms_url` и без выжимки условий в докстринге от трёх строк. Отсутствие
robots.txt (404) означает «разрешено» — так отвечает `api.hh.ru`.
`BLOCKED_HOSTS` в транспорте отбивает LinkedIn, Indeed и Glassdoor независимо
от того, что объявил коннектор. У `hh` закрыт не хост, а то, что запрещает его
`robots.txt`: любой URL со строкой запроса, `/search` и `api.hh.ru/vacancies`.
Это отдельная проверка, потому что `urllib.robotparser` не понимает
подстановочных знаков и правило `Disallow: *?*` у него не срабатывает вовсе.

### Планировщик запросов

Наивный план «каждый скилл × каждый регион» даёт 60 запросов на профиле из
20 скиллов и трёх мест поиска, а выдачи почти полностью пересекаются. План
строится из групп словаря `skills_min.yaml` (у всех 104 записей есть `group`),
кросс групп и мест обходится по диагонали, дубли схлопываются, и результат
**усекается** до `MAX_QUERIES_PER_RUN` — отброшенные запросы не возвращаются
вообще, поэтому их некому выполнить. На реальном профиле: 60 → 8.

Группа `language` получает место в плане вне ранжирования. Это измерение, а не
предпочтение: на 36 живых вакансиях `python` встретился в 9 описаниях, а
`django`, `fastapi` и `sqlalchemy` — ни в одном. Вес группы растёт от её
размера, а язык у кандидата один, поэтому без исключения план искал бы по трём
словам, которых в вакансиях нет.

### Прогон

`pipeline/runner.py` разводит источники параллельно, каждому — своя сессия и
свой лимитер. Падение источника не роняет прогон: ошибка ложится в
`pipeline_run.errors`, а статус становится `partial`, а не `failed` — потому
что `last_successful()` считает `partial` водяным знаком, и «провален» сбросил
бы инкрементальное окно всем остальным.

Ожидание — не отказ. Кулдаун (`min_interval`) и исчерпанная дневная квота
(`daily_quota`, счёт в `source_quota`) отражаются в `GET /sources` как
`cooling_down` и `quota_exhausted` со временем следующей попытки. Строка
`pipeline_run` для пропущенного источника не создаётся: статуса `SKIPPED` нет,
а `ALTER TYPE` на enum — известная опасность миграции. Кредит списывается
**при отправке** запроса, а не при успехе: ответ 500 у RapidAPI уже оплачен.

Источник, чей эндпоинт не принимает параметров поиска, обязан переопределить
`search_batch` и забирать ленту один раз на весь план. Живой прогон без этого
сделал 8 запросов к remotive при разрешённых условиями ~4 в сутки и 182
запроса к arbeitnow вместо 9.

### Эмбеддинги в прогоне

Считаются после дедупликации, по уже записанным строкам, и никогда для
неизменившегося описания: перекраул переписывает `updated_at` независимо от
того, поменялось ли слово, поэтому признаком служит sha256 самого текста в
`vacancy.embedding_text_hash`. Шаг имеет право ничего не сделать —
`sentence-transformers` необязателен, и успешно собранный прогон не должен
считаться провальным из-за того, что векторы не посчитались.

**Каждый батч коммитится отдельно, а не весь расчёт в конце.** Это не деталь
реализации, а исправление конкретной аварии: старая версия считала весь остаток
одним вызовом и писала одним стейтментом в самом конце, поэтому любая остановка
процесса выбрасывала всё, что было посчитано. Bge-m3 на CPU этой машины — около
семи секунд на вакансию, то есть 466 строк это 54 минуты, а корпус одного
города — больше суток. Наблюдалось ровно это: 466 вакансий в базе, ноль
векторов, и 466 готовых векторов в `.cache/embeddings`. Теперь прерванный
проход сохраняет всё, что успел, а следующий продолжает с того же места.

Проход ограничен двумя независимыми бюджетами — `EMBEDDING_MAX_PER_RUN` и
`EMBEDDING_TIME_BUDGET_SECONDS` — и оба проверяются между батчами, чтобы батч
никогда не бросали посчитанным наполовину. Поэтому проход штатно заканчивается
недоделанным, и остаток он обязан назвать: `EmbeddingOutcome.backlog` — это
отдельный `COUNT` по тому же предикату, что и выборка, а не остаток текущего
окна. Окно (200 строк) меньше строчного лимита (2000), так что «сколько
осталось в окне» на больших backlog'ах всегда ноль — то есть ровно тот случай,
ради которого число и нужно.

Догнать backlog, не запуская краул: `make embed-backlog`
(`scripts/embed_backlog.py`, `--until-drained` для нескольких проходов подряд).

### `pipeline/`
- `runner.py` — оркестрация прогона.
- `embedding.py` — расчёт векторов батчами с коммитом после каждого батча.

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

### `llm/`

Три провайдера за одним протоколом (`app/llm/base.py`), выбор — по типу задачи
из конфига, не по деплою.

| Задача | Провайдер | Почему |
| --- | --- | --- |
| `resume_extraction` | `cli` | раз на загрузку, качество решает всё дальше; подписка уже оплачена |
| `cover_letter`, `tooling` | `cli` | то же самое |
| `telegram_parse` | `ollama` | сотни за ночь, задача простая, никто не ждёт |
| `vacancy_parse`, `rerank` | `api` | нужна латентность, кэш промта и structured outputs |

**Арифметика, а не вкусовщина.** Вызов CLI несёт ~50 000 токенов системного
промта Claude Code независимо от полезной нагрузки — замерено: $0.064 на
промте, ответ которого `{"ok": true}`, и $0.21 на настоящем резюме. Раз в
загрузку это нормально, на re-rank (120 вызовов в сутки) это ~$44/сутки
подписочной квоты.

Три вещи в CLI-провайдере несущие:

- **Никогда не shell и никогда не в argv.** Промт уходит в stdin. Аргументы
  видны в списке процессов любому на машине, а там текст резюме. Плюс на
  Windows `claude` — это `.CMD`, то есть лимит `cmd.exe` в 8 191 символ:
  настоящий промт извлечения ~7 900 символов и падал с «слишком длинная
  командная строка». Через stdin командная строка — 198 символов.
- **Бинарь резолвится, а не называется.** `create_subprocess_exec("claude")`
  падает на Windows с FileNotFoundError: exec не делает разбор PATHEXT.
  `shutil.which` — один раз при старте.
- **Инструменты выдаются по задаче, таблицей** (`TOOL_POLICY`). `Read` есть
  только у `resume_extraction`, и только в каталоге, где лежит один этот файл.
  У остальных инструментов нет вовсе: в промт сопроводительного письма
  подставляется описание вакансии из интернета, то есть недоверенный текст
  в агенте с доступом к файловой системе.

Фолбэк по цепочке пишет **WARNING**. Молчаливое переключение положило бы в один
датасет два качества извлечения без следа.

Учёт (`app/llm/usage.py`) разделяет `measured` (счёт от API), `subscription`
(квота уже оплаченного плана) и `estimated`. В одну сумму они не складываются:
результат не был бы ни счётом, ни расходом, но выглядел бы как деньги.

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
  salary_min_normalized, salary_max_normalized, salary_normalized_at,
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

### Решения по типам

- **Деньги — `Numeric(12,2)`, никогда не float.** Ошибка двоичного float
  накапливается при конвертации валют.
- **Все скоры — одна шкала 0–100, `Numeric(5,2)`**, включая компонентные
  (в `docs/MATCHING.md` они описаны как доли 0..1 — нормализуются при записи).
- **`currency`, `country`, `language` — строки фиксированной длины**, не native
  enum: списки пополняются, а `ALTER TYPE` в PostgreSQL — источник проблем при
  миграции. Валидация ISO 4217 / 3166 / 639 на уровне Pydantic.
- **Сортировка и фильтр по зарплате идут по `salary_*_normalized`** (месячный
  эквивалент в USD), никогда по объявленной сумме: `500000 ₸` иначе окажется
  «выше» `4000 $`. Колонки заполняются на этапе нормализации; вакансии без
  известного курса просто уезжают в конец выдачи, а не сортируются неверно.
- **Все enum'ы — native-типы PostgreSQL.** Alembic autogenerate их не создаёт и
  не удаляет, поэтому `CREATE TYPE` / `DROP TYPE` в миграциях пишутся явно, а
  в моделях стоит `create_type=False`.
- **`vacancy.search_vector` — generated column** с конфигурацией `'simple'`.
  Вакансии смешанные ru/en, а `to_tsvector(regconfig, text)` не IMMUTABLE,
  поэтому выбрать конфигурацию по языку строки в generated-колонке нельзя.
  Стемминг по языку, если понадобится, — отдельный expression-индекс.

### Индексы

Индексы, которые нельзя выразить в метаданных ORM, пишутся сырым SQL и носят
префикс `ix_pg_`; `env.py` исключает этот префикс из autogenerate, иначе он
предлагал бы удалять их при каждой генерации.

| Индекс | Тип |
| --- | --- |
| `ix_pg_vacancy_embedding_hnsw` | HNSW, `vector_cosine_ops`, m=16, ef_construction=64 |
| `ix_pg_candidate_profile_embedding_hnsw` | HNSW, те же параметры |
| `ix_pg_vacancy_search_vector_gin` | GIN по generated-колонке |
| `ix_pg_vacancy_published_at_active` | btree `(published_at DESC NULLS LAST, id DESC) WHERE is_active` |
| `ix_pg_vacancy_salary_normalized_active` | btree `(salary_min_normalized DESC NULLS LAST, id DESC) WHERE is_active` |
| `ix_pg_match_profile_score` | btree `(profile_id, score DESC, vacancy_id DESC)` |

### Пагинация

Список вакансий — **keyset, без `OFFSET`**. Курсор кодирует пару
`(значение сортировки, id)` в base64. Два обязательных условия, без которых
баг не воспроизводится на аккуратных данных:

1. **Tiebreaker по `id`.** Сотни вакансий имеют одинаковый скор; без
   составного сравнения `(score, id)` записи пропадают между страницами.
2. **Отдельная ветка для NULL.** `score`, `published_at` и
   `salary_min_normalized` nullable, а `value < NULL` — это NULL, то есть
   false. Порядок — `NULLS LAST`, и курсор несёт явный флаг «мы уже в
   NULL-хвосте».

## API (v1)

Что реализовано:

```
POST   /api/v1/resume/upload          multipart → profile_id, задача разбора
GET    /api/v1/profile/active         активное резюме, без знания его id
GET    /api/v1/profile/{id}
GET    /api/v1/profile/{id}/ats-report   прочитает ли резюме робот работодателя
PATCH  /api/v1/profile/{id}           ручная правка скиллов/предпочтений

GET    /api/v1/overview               экран «Обзор» целиком, одним запросом
GET    /api/v1/vacancies              фильтры + keyset-пагинация + фасеты
GET    /api/v1/vacancies/{id}         карточка: score, требования, письмо
GET    /api/v1/tracker/board          канбан откликов: этапы и исходы
GET    /api/v1/documents              резюме с ATS-отчётом и все письма
GET    /api/v1/documents/queue        вакансии, которым стоит написать письмо
POST   /api/v1/documents/letters      написать письмо и сохранить его

GET    /api/v1/sources                список, статус, лимиты, причина неактивности
POST   /api/v1/pipeline/run           ставит краул в очередь, отдаёт job
GET    /api/v1/pipeline/jobs[/{id}]   опрос запущенного краула
GET    /api/v1/pipeline/runs[/{id}]   история прогонов

GET    /api/v1/applications/queue     ← локальный агент, только по токену
POST   /api/v1/applications/results   ← он же, отчёт об отправке
```

Ещё не написано: `POST /profile/{id}/rescore`, `/analytics/skill-gaps`,
`/analytics/salary`, ручное редактирование трекера.

**Дашборд не отправляет отклики и не будет.** Отправка — `wwao apply --send`,
где письмо печатается на карточке подтверждения и человек за клавиатурой
говорит «да» этому письму для этой вакансии; браузер такой гарантии не даёт.
Единственная запись, которую делает дашборд, — `POST /documents/letters`, и она
создаёт документ. Приложение-агент живёт за отдельным префиксом
`/applications` под локальным токеном именно поэтому: экран и шов к агенту не
делят пространство имён.

Фильтры `/vacancies` — одна Pydantic-модель `VacancyQuery`, разложенная в
query-параметры: `score_min`, `score_max`, `bucket`, `source`, `remote`,
`city`, `country`, `salary_min`, `include_unpriced`, `currency`, `seniority`,
`posted_within_days`, `has_salary`, `missing_skills_max`, `company`, `q`
(полнотекст), `exclude_applied`, `include_filtered`, `sort`
(`score|published_at|salary`), `direction`, плюс `cursor`, `limit`,
`with_total`, `with_facets`.

Две подробности этой модели стоят того, чтобы их знать.

*`include_unpriced` по умолчанию `true`.* `salary_min_normalized >= x` ложно для
NULL, а пять вакансий из шести на этом корпусе зарплату не называют, так что
порог сам по себе отвечает шестой — молча. Читающий такой список делает вывод о
рынке, а не о поле. `include_unpriced=false` — это другой вопрос, и его задают
осознанно.

*Модель одна на весь хендлер, и это не стиль.* FastAPI раскладывает
Pydantic-модель в отдельные query-параметры только пока она — единственное
query-поле обработчика. Рядом с обычным `limit` та же модель молча становится
одним непрозрачным параметром: все фильтры игнорируются, ответ 200. Поэтому
пагинация лежит внутри `VacancyQuery`, а `.filters()` отдаёт репозиторию только
фильтрующую половину.
