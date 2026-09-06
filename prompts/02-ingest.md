# Фазы 2–4. Сбор данных

---

## Промт фазы 2 — резюме → профиль

```
Фаза 2. Прочитай CLAUDE.md, docs/ARCHITECTURE.md (раздел resume/) и
docs/MATCHING.md. Слой данных из фазы 1 готов.

ЗАДАЧА: превратить загруженный файл резюме в структурированный
CandidateProfile с эмбеддингом.

1. app/resume/extractor.py
   - PDF: pdfplumber, фолбэк pypdf при пустом тексте
   - если после обоих текста < 200 символов — считаем сканом, OCR через
     pytesseract (языки rus+eng+kaz), это опциональная зависимость,
     её отсутствие обрабатывается понятной ошибкой
   - DOCX: python-docx, включая таблицы
   - TXT/MD: как есть
   - валидация: размер ≤ 10 МБ, разрешённые MIME, защита от zip-бомб
   - возвращает ExtractedDocument(text, page_count, source_format, warnings)

2. app/llm/client.py — обёртка над Anthropic SDK:
   - модель из settings, счётчик токенов и стоимости в лог
   - метод complete_json(prompt_name, variables, response_model) →
     валидирует ответ Pydantic-моделью, при ошибке один ретрай с текстом
     ошибки валидации, потом LLMError
   - таймауты и tenacity-ретраи на 429/5xx
   - промты грузятся из app/llm/prompts/*.md, не инлайном

3. app/llm/prompts/extract_profile.md — промт извлечения. Требует строгий
   JSON без преамбулы. Извлекает: имя, headline, суммарный опыт в годах
   (считать по датам работ, а не по заявлению), seniority, список мест
   работы с датами и стеком, скиллы с уровнем и годами использования и
   годом последнего использования, домены, языки с уровнем, город и страну,
   готовность к релокации, предпочтение по формату работы, зарплатные
   ожидания, образование. Резюме может быть на русском, английском или
   казахском — определяй язык сам, ответ всегда в одной схеме.

4. app/resume/profile_builder.py — оркестрация: extractor → LLM → Pydantic
   ProfileExtraction → сохранение CandidateProfile + ProfileSkill.

5. app/resume/enricher.py
   - канонизация названий скиллов по normalize/skills.yaml
   - пересчёт years_per_skill по датам работ, где скилл упомянут
   - вывод seniority, если LLM не определил
   - флаг stale для скиллов, не использовавшихся > 3 лет

6. app/matching/embeddings.py — сервис эмбеддингов:
   - BAAI/bge-m3 через sentence-transformers, ленивая загрузка модели,
     синглтон, инференс в threadpool чтобы не блокировать event loop
   - encode_profile: собирает текст из headline + скиллы + опыт, не сырое
     резюме целиком
   - encode_vacancy: title + требования + описание, обрезка по токенам
   - интерфейс EmbeddingProvider, чтобы модель можно было подменить

7. API: POST /api/v1/resume/upload (multipart) → 202 + profile_id, разбор
   в фоне через BackgroundTasks, статус в GET /api/v1/profile/{id}
   (поле parse_status: pending|ready|failed + error).
   PATCH /api/v1/profile/{id} — ручная правка скиллов, локаций, ожиданий.

8. Тесты: реальные PDF и DOCX фикстуры в tests/fixtures/resumes/,
   LLM замокан, проверка что невалидный JSON от LLM приводит к ретраю,
   а второй невалидный — к LLMError; проверка канонизации скиллов;
   проверка отказа на файле 20 МБ и на .exe.

DoD из CLAUDE.md. Сначала план, потом код.
```

---

## Промт фазы 3 — фреймворк коннекторов и первые источники

```
Фаза 3. Прочитай CLAUDE.md, docs/SOURCES.md, docs/ARCHITECTURE.md
(раздел sources/).

ЗАДАЧА: расширяемый фреймворк источников + три рабочих коннектора.
Ключевое требование: добавление нового источника не должно требовать
изменений НИ В ОДНОМ файле вне sources/.

1. app/sources/base.py
   - SearchQuery: keywords list[str], area, country, remote, salary_min,
     posted_within_days, employment_type, language, limit
   - RawPosting: source_slug, external_id, url, title, company,
     description (Optional), raw dict, fetched_at
   - RateLimit: requests_per_second, burst; реализация через асинхронный
     token bucket
   - BaseSource(ABC): атрибуты slug, name, regions, requires_auth,
     rate_limit, needs_detail_fetch; методы is_configured(),
     search(query) -> AsyncIterator[RawPosting], fetch_detail(posting)

2. app/sources/registry.py — декоратор @register_source, реестр,
   автоимпорт всех модулей пакета через pkgutil, метод
   get_enabled_sources() учитывающий is_configured() и конфиг включённости.

3. app/sources/http.py — общий асинхронный клиент:
   httpx.AsyncClient с таймаутом и User-Agent из settings, token-bucket
   rate limiter на источник, tenacity-ретраи (429/5xx, уважает Retry-After,
   максимум 4 попытки), проверка robots.txt с суточным кэшем,
   опциональный дисковый кэш ответов в dev (переменная HTTP_CACHE_DIR).

4. Коннекторы:

   a) app/sources/hh.py — slug "hh".
      УСТАРЕЛО (06.09.2026). Путь через GET https://api.hh.ru/vacancies
      мёртв: 403 любому программному клиенту без ключа работодателя.
      Дробление окон при found > 2000 было нужно только поисковой выдаче
      этого API и не реализовано.
      Коннектор написан по другому заданию — MEGAPROMPT-hh-sitemap.md:
      карта сайта <город>.hh.kz/sitemap/vacancy{N}.xml, затем
      /vacancy/{id} без query-строки, разбор состояния фронтенда из
      HH-Lux-InitialState. Справочники /areas и /professional_roles на
      api.hh.ru по-прежнему открыты и остаются источником нормализации.

   b) app/sources/remotive.py — slug "remotive".
      GET https://remotive.com/api/remote-jobs, описание приходит сразу,
      кэш минимум на 6 часов, фильтрация по категории и ключевым словам
      на нашей стороне.

   c) app/sources/arbeitnow.py — slug "arbeitnow".
      GET https://www.arbeitnow.com/api/job-board-api с пагинацией по page,
      парсинг тегов (visa_sponsorship, remote).

5. app/pipeline/runner.py — прогон плана поиска:
   - строит SearchQuery из профиля (ключевые слова = топ-скиллы + headline,
     регионы = локация + релокация + remote)
   - параллельно по источникам, семафор на каждый источник отдельно
   - падение одного источника не роняет прогон, ошибка в PipelineRun.errors
   - пишет метрики: found / new / updated / errors на источник
   - POST /api/v1/pipeline/run для ручного запуска (опционально список
     source_slug), GET /api/v1/pipeline/runs для истории,
     GET /api/v1/sources для списка и статуса

6. Тесты: для каждого коннектора — зафиксированный JSON-ответ в
   tests/fixtures/{slug}.json, HTTP замокан через respx, проверка что
   поля разложились правильно; тест на rate limiter (не превышает rps);
   тест на ретрай при 429; тест что реестр подхватывает новый источник
   без правок пайплайна; тест что упавший источник не ломает прогон.

ЗАПРЕЩЕНО: коннекторы к LinkedIn, Indeed, Glassdoor. Обход капчи и
антибот-защиты. Отключение rate limiting.

DoD из CLAUDE.md. Сначала план, потом код.
```

---

## Промт фазы 4 — нормализация и дедупликация

```
Фаза 4. Прочитай CLAUDE.md, docs/ARCHITECTURE.md (normalize/),
docs/MATCHING.md. Коннекторы из фазы 3 отдают RawPosting.

ЗАДАЧА: превратить разнородные RawPosting в единый чистый Vacancy
без дублей.

1. app/normalize/skills.yaml — канонический словарь навыков.
   Формат:
     postgresql:
       aliases: [postgres, pg, постгрес, postgre]
       group: database
       related: {mysql: 0.6, clickhouse: 0.4, sqlite: 0.5}
   Наполни минимум 250 позициями: языки, бэкенд/фронтенд фреймворки,
   БД, очереди, devops, облака, ML, мобильная разработка, инструменты.
   Русские и английские варианты обязательно. Плюс стоп-лист мусорных
   «скиллов» (коммуникабельность, стрессоустойчивость) — они не участвуют
   в скоринге, но собираются как red flags.

2. app/normalize/skill_matcher.py — извлечение скиллов из текста:
   поиск по алиасам с учётом границ слов и морфологии, регистронезависимо,
   защита от ложных срабатываний (C, R, Go как отдельные буквы/слова —
   только в техническом контексте). Определение, требование это или
   «будет плюсом», по блоку текста, в котором найден скилл.
   Возвращает list[ExtractedSkill(canonical, raw, is_required, weight)].

3. app/normalize/salary.py — парсер зарплат:
   форматы «от 500 000 ₸», «300–450 тыс. руб», «$4000-6000", «60k-80k EUR",
   «по договорённости», указание gross/net/на руки/до вычета,
   период (месяц/год/час). Нормализация валюты по ISO, приведение к
   месячному эквиваленту для сравнения, курсы из конфига с возможностью
   обновления. Возвращает SalaryRange или None.

4. app/normalize/mapper.py — RawPosting → Vacancy:
   - HTML → markdown (описание hh приходит в HTML)
   - определение языка вакансии (langdetect)
   - извлечение требуемого опыта в годах из текста и из полей источника
   - определение seniority по заголовку и тексту
   - определение remote/hybrid/office
   - извлечение домена компании
   - вызов skill_matcher и salary parser
   Всё что не удалось распарсить — None, а не выдуманное значение.

5. app/normalize/dedup.py
   - fingerprint = sha1(normalized_company | normalized_title | city);
     нормализация: lower, убрать ООО/ТОО/LLC/Inc, схлопнуть пробелы,
     убрать грейд из заголовка
   - simhash по описанию для случаев с разными заголовками; порог
     расстояния Хэмминга в конфиге
   - при совпадении: одна Vacancy, несколько VacancySource; выбираем
     лучшее описание (самое длинное), объединяем скиллы, берём максимально
     полную зарплатную вилку, published_at — самый ранний

6. Интеграция в пайплайн: RawPosting → mapper → dedup → upsert.
   Существующая вакансия обновляет last_seen_at; пропавшая из выдачи
   более N дней помечается is_active=False, а не удаляется.

7. Тесты: параметризованные тесты парсера зарплат на 30+ реальных строках
   (русские, английские, тенге/рубли/доллары/евро); тест извлечения
   скиллов с ловушками («опыт работы с C-уровнем менеджмента» не должно
   дать скилл C); тест дедупа на трёх вариантах одной вакансии из hh,
   greenhouse и telegram; тест что повторный прогон не создаёт дублей.

DoD из CLAUDE.md. Сначала план, потом код.
```
