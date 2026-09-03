# Фазы 5–6. Соответствие и API

---

## Промт фазы 5 — движок соответствия

```
Фаза 5. Прочитай CLAUDE.md и docs/MATCHING.md — реализуй спецификацию
оттуда буквально, без импровизации в формулах.

ЗАДАЧА: объяснимый скоринг вакансии относительно профиля.

1. app/matching/config.py — MatchingConfig на pydantic-settings с весами
   компонентов, порогами бакетов, штрафами soft-fail, top_n для LLM,
   max_age_days. Значения по умолчанию из docs/MATCHING.md. Веса должны
   меняться без правки кода; добавь валидатор, что сумма весов = 1.0.

2. app/matching/filters.py — жёсткие фильтры из раздела «Этап 0».
   Возвращает FilterResult(passed: bool, hard_fail_reason, soft_penalties).
   Никаких «молча выбросил» — причина всегда фиксируется и сохраняется.

3. app/matching/rules.py — компоненты rule-скора отдельными чистыми
   функциями, каждая возвращает значение 0..1 плюс объяснение:
   - skill_coverage(required/nice) с таблицей have() из docs/MATCHING.md,
     включая граф related из skills.yaml и множитель уровня владения
   - experience_fit по таблице gap
   - domain_fit
   - logistics_fit
   Каждая функция чистая и тестируется изолированно.

4. app/matching/semantic.py — косинус между эмбеддингами через pgvector.
   Метод prefilter_candidates(profile_id, limit) — достаёт top-K вакансий
   одним индексным запросом по HNSW, чтобы не скорить весь корпус.

5. app/matching/llm_rerank.py + app/llm/prompts/rerank_vacancy.md
   - применяется только к top_n вакансиям с rule_score >= 50
   - на вход: структурированный профиль, полный текст вакансии,
     rule-разбор; на выход строго схема из docs/MATCHING.md
   - вердикт и application_angle — на русском языке
   - невалидный JSON → один ретрай → фолбэк на rule-only, флаг
     llm_failed в Match
   - батчинг и подсчёт токенов, лимит стоимости на прогон из конфига

6. app/matching/engine.py — оркестратор:
   filters → rules → semantic → (top_n) llm → final score → bucket →
   bulk upsert в match. Идемпотентно: повторный прогон обновляет,
   не плодит. Метод rescore_profile(profile_id, force: bool).

7. Объяснимость: Match всегда содержит заполненные matched_skills,
   missing_required, missing_nice, experience_gap_years, component_scores
   (разбивка по каждому слагаемому), verdict, red_flags.
   Скор без разбора считать багом.

8. Эндпоинты: POST /api/v1/profile/{id}/rescore,
   GET /api/v1/vacancies/{id}/match.

9. Тесты — здесь особенно важны:
   - золотой набор: 15 вручную размеченных пар «профиль-вакансия» с
     ожидаемым бакетом; тест проверяет, что движок попадает в бакет
   - каждая rule-функция параметризованно, включая граничные значения
     (gap ровно 0, ровно 4, ровно 4.1)
   - hard-fail не выбрасывает запись, а помечает
   - LLM замокан; проверка фолбэка при двух невалидных ответах
   - проверка, что сумма весов валидируется и кривой конфиг падает на старте

DoD из CLAUDE.md. Сначала план, потом код.
```

---

## Промт фазы 6 — публичный API

```
Фаза 6. Прочитай CLAUDE.md и docs/ARCHITECTURE.md (раздел API v1).
Движок из фазы 5 работает.

ЗАДАЧА: полный REST API, на котором можно построить фронт без доработок
бэка.

1. Реализуй все эндпоинты из docs/ARCHITECTURE.md. Роутеры в
   app/api/v1/, разбиты по доменам: resume, profile, vacancies, sources,
   pipeline, applications, analytics.

2. GET /api/v1/vacancies — главный эндпоинт:
   - все фильтры из документации через один Pydantic Query-объект
     VacancyFilter с валидацией диапазонов и понятными 422
   - сортировка score|published_at|salary, направление asc/desc
   - keyset-пагинация (cursor), не offset — корпус будет расти
   - ответ VacancyListItem: только то, что нужно таблице
     (id, title, company, source_slugs, city, remote, salary, score,
     bucket, missing_required_count, published_at, is_applied)
   - агрегаты в ответе: total, facets по source/bucket/city для
     отображения счётчиков в фильтрах

3. GET /api/v1/vacancies/{id} — полная карточка с описанием,
   всеми источниками, разбором соответствия, историей.

4. POST /api/v1/vacancies/{id}/cover-letter — генерация сопроводительного
   письма. Промт в app/llm/prompts/cover_letter.md: на вход профиль,
   вакансия, application_angle из матча; на выходе письмо на языке
   вакансии, 150–200 слов, без канцелярита и без выдуманного опыта.
   Параметры: tone (formal|neutral|direct), language override.
   Результат сохраняется в Application.

5. Applications: CRUD + смена статуса, канбан-выборка
   GET /api/v1/applications?group_by=status.

6. Analytics:
   - GET /analytics/skill-gaps — агрегация missing_required по вакансиям
     со score >= порога; на каждый скилл: сколько вакансий откроется,
     медианная вилка этих вакансий, средний прирост скора. Сортировка
     по влиянию
   - GET /analytics/salary — распределение вилок по бакетам, по городам,
     по remote/office
   - GET /analytics/overview — счётчики для главной: всего вакансий,
     новых за 24ч, по бакетам, последний прогон

7. OpenAPI: осмысленные summary, description, теги, примеры ответов.
   Схема должна читаться как документация.

8. Обработка ошибок: единый формат problem+json, никаких утечек
   стектрейсов в проде. 404 для отсутствующих сущностей, 409 для
   конфликтов, 422 для валидации.

9. Тесты: интеграционные на каждый эндпоинт через httpx ASGITransport;
   параметризованный тест на каждый фильтр списка вакансий; тест
   keyset-пагинации на 200 записях (нет пропусков и дублей при вставке
   между страницами); тест генерации письма с замоканным LLM.

DoD из CLAUDE.md. Сначала план, потом код.
```
