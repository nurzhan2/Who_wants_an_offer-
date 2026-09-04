# Каталог источников

Приоритет: официальный API без ключа → API с бесплатным ключом → HTML там,
где `robots.txt` разрешает. Источники с явным запретом автосбора не
подключаем — вместо них используем легальные обходные пути (см. LinkedIn).

## Ядро

Три источника, на которых система должна работать даже если все остальные
отвалятся. Реализуются в фазе 3, до всего остального.

| slug | Тип | Регион | Почему в ядре |
| --- | --- | --- | --- |
| `hh` | Официальный API | KZ, RU, BY, UZ, KG, AZ, GE | Максимальное покрытие рынка СНГ, структурированные данные, бесплатно |
| `telegram` | Telethon, публичные каналы | KZ, RU | Половина реальных IT-вакансий в регионе не доходит до job-бордов; в каналах они появляются первыми и часто с вилкой |
| `jsearch` | RapidAPI → Google for Jobs | Глобально | Легальный доступ к постингам, которые видны на LinkedIn и Indeed |

---

## LinkedIn: почему автопарсера нет и что вместо него

**Автоматический сбор с LinkedIn не реализуется.** Причина практическая, не
этическая: LinkedIn детектит автоматизацию и банит аккаунты. Потерять свой
профиль в разгар поиска работы — цена несопоставимая с выигрышем. Официальный
Talent Solutions API выдаётся только партнёрам-вендорам, физлицу его не
получить. Судебная практика (hiQ v. LinkedIn) закончилась победой LinkedIn по
нарушению условий использования.

Три легальных пути к тем же вакансиям:

### 1. `jsearch` — автоматический, покрывает большую часть
RapidAPI, агрегирует Google for Jobs. Google индексирует LinkedIn-постинги
через `JobPosting` schema.org разметку, которую LinkedIn публикует сам.
Эндпоинт `GET https://jsearch.p.rapidapi.com/search`, параметры `query`,
`page`, `country`, `date_posted`, `remote_jobs_only`. Бесплатный тариф —
несколько сотен запросов в месяц, платный дешёвый. Ключ в `RAPIDAPI_KEY`.

### 2. ATS-борды — первоисточник
Существенная доля вакансий на LinkedIn — ретрансляция из Greenhouse, Lever,
Ashby, Workable. Идём напрямую в ATS: свежее, полнее, с полным описанием и
часто с вилкой, которой на LinkedIn нет. См. Tier 1 ниже.

### 3. `linkedin_manual` — ручной импорт
Не коннектор, а эндпоинт `POST /api/v1/vacancies/import`, принимающий:
- `url` — ссылка на вакансию, система тянет только эту одну страницу
- `text` / `html` — вставленный вручную текст вакансии
- CSV из официального экспорта LinkedIn («Settings → Get a copy of your
  data → Saved jobs»)

Текст разбирается тем же LLM-нормализатором, вакансия попадает в общий
скоринг с `source_slug = linkedin_manual`. Один клик на вакансию,
инициатива всегда пользовательская.

**Явно запрещено:** превращать `linkedin_manual` в краулер, ходить по
списку выдачи, использовать сессионные куки, обходить антибот-защиту.
Ограничение на уровне кода: не более одного URL за вызов, без рекурсии по
ссылкам, без хранения авторизационных заголовков.

Индеед и Glassdoor — по той же схеме: автопарсера нет, покрытие через
`jsearch` и ручной импорт.

---

## Tier 1 — официальный API, без ключа

| slug | Эндпоинт | Регион | Заметки |
| --- | --- | --- | --- |
| `hh` | `GET https://api.hh.ru/vacancies` | KZ, RU, BY, UZ, AZ, KG, GE | `host=hh.kz` для Казахстана. `area`: 40 Казахстан, 160 Алматы, 159 Астана, 113 Россия, 1 Москва. `per_page ≤ 100`, `page ≤ 19` → потолок 2000 на запрос, обходится дроблением по `date_from/date_to` и `professional_role`. Описание только в `/vacancies/{id}`. Обязателен осмысленный User-Agent. Справочники: `/areas`, `/professional_roles`, `/dictionaries` |
| `remotive` | `GET https://remotive.com/api/remote-jobs` | remote | Описание сразу, просят кэшировать, не чаще раза в сутки |
| `remoteok` | `GET https://remoteok.com/api` | remote | Первый элемент — юридическая заглушка, пропускать |
| `arbeitnow` | `GET https://www.arbeitnow.com/api/job-board-api` | DE, EU | Пагинация `?page=`, теги `visa_sponsorship`, много релокейта |
| `himalayas` | `GET https://himalayas.app/jobs/api?limit=100&offset=0` | remote | |
| `jobicy` | `GET https://jobicy.com/api/v2/remote-jobs` | remote | |
| `weworkremotely` | RSS `https://weworkremotely.com/remote-jobs.rss` + категорийные фиды | remote | RSS, парсится `feedparser`, описание в `<description>` как HTML |
| `hn_hiring` | `GET https://hn.algolia.com/api/v1/search?tags=comment,story_{id}` | remote, US, EU | Ежемесячный тред «Ask HN: Who is hiring». Находим тред через поиск по заголовку, тянем комментарии верхнего уровня, каждый = вакансия, структурируем LLM. Очень качественные remote-позиции |
| `eures` | EURES, портал трудовой мобильности ЕС, публичный API | EU/EEA | Официальный источник, релевантен для релокации в Европу |
| `cryptojobslist` | `GET https://cryptojobslist.com/api/jobs` | remote, crypto | |
| `greenhouse` | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | per-company | |
| `lever` | `GET https://api.lever.co/v0/postings/{company}?mode=json` | per-company | |
| `ashby` | `GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | per-company | Часто отдаёт вилку |
| `recruitee` | `GET https://{company}.recruitee.com/api/offers/` | EU | |
| `workable` | `GET https://apply.workable.com/api/v1/jobs?token={account}` | per-company | |
| `smartrecruiters` | `GET https://api.smartrecruiters.com/v1/companies/{company}/postings` | per-company | |
| `personio` | `GET https://{company}.jobs.personio.de/xml` | DE | XML |
| `teamtailor` | `https://{company}.teamtailor.com/jobs.json` | EU/Nordics | |
| `freehire` | `GET https://freehire.me/api` (базовый URL — в конфиг) | мультирынок | Публичный REST, JSON, без ключа. Техвакансии: разработка, данные, DevOps, remote. Выдача уже структурирована — скиллы, грейд, категория — то есть нормализация дешевле, чем на RSS-источниках. Бэкенд MIT и self-hostable, поэтому базовый URL держим в конфиге: инстанс может переехать или быть поднят своим |

ATS-источники требуют списка компаний — `app/sources/ats/company_boards.yaml`.
Это самый качественный канал: данные из первых рук, без посредников и
задержек. Список расширяется постоянно.

## Tier 2 — API с бесплатным ключом

| slug | Эндпоинт | Регион | Лимиты |
| --- | --- | --- | --- |
| `jsearch` | `GET https://jsearch.p.rapidapi.com/search` | глобально | RapidAPI, бесплатный тариф ограничен, считать вызовы |
| `adzuna` | `GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}` | GB, DE, PL, NL, AT, FR, IT, ES, US и др. | `app_id`+`app_key`, ~250 запросов/сутки |
| `jooble` | `POST https://jooble.org/api/{key}` | 70 стран, включая KZ и RU | ключ по заявке |
| `careerjet` | `GET http://public.api.careerjet.net/search` | 90 стран, есть KZ и RU | нужен affiliate id, бесплатный |
| `themuse` | `GET https://www.themuse.com/api/public/jobs?page=` | US, EU | |
| `findwork` | `GET https://findwork.dev/api/jobs/` | remote, US | токен |

## Tier 3 — HTML / внутренний JSON

Перед реализацией каждого — проверить `robots.txt` и зафиксировать в
докстринге класса, что именно разрешено. Запрещено — не реализуем.

| slug | Источник | Регион |
| --- | --- | --- |
| `habr` | `career.habr.com/vacancies` | RU |
| `getmatch` | `getmatch.ru`, внутренний JSON-эндпоинт, вилка почти всегда | RU, релокация |
| `djinni` | `djinni.co/jobs/` | UA, EU, remote |
| `enbek` | `enbek.kz`, государственный портал РК | KZ |
| `relocate_me` | `relocate.me` — вакансии с релокационным пакетом | EU, глобально |
| `landing_jobs` | `landing.jobs` | PT, EU |

---

## `telegram` — подробно

Отдельный промт: `prompts/03b-telegram.md`. Ключевые решения:

- **Telethon с user-сессией**, не Bot API. Бот не может читать произвольный
  публичный канал, не будучи админом; пользовательская сессия может читать
  публичные каналы по `@username` без вступления.
- **Список каналов** в `app/sources/telegram_channels.yaml` с метаданными:
  регион, стек, язык, тип (вакансии / фриланс / релокация).
- **Двухступенчатая фильтрация**: дешёвая эвристика отсекает 80% постов
  (не вакансия), LLM разбирает только оставшееся. Иначе стоимость улетает.
- **Инкрементальность**: хранится `last_message_id` на канал, при следующем
  прогоне читаем только новое.
- **Дедуп** с остальными источниками через общий fingerprint — один и тот же
  оффер часто висит и на hh, и в канале.
- **FloodWait** обрабатывается обязательно, иначе Telegram ограничит аккаунт.
- **Сессия не в репозитории**, права 600, отдельный аккаунт под это.

---

## Правила для любого коннектора

1. Читать и уважать `robots.txt`, кэш на 24 часа.
2. `User-Agent: who-wants-an-offer/1.0 (+https://github.com/nurzhan2/Who_wants_an_offer-)`.
3. Rate limit задан в классе коннектора, по умолчанию 1 rps.
4. Ретраи с экспоненциальным backoff на 429/5xx, максимум 4 попытки,
   уважать `Retry-After`.
5. Дисковый кэш ответов в dev-режиме.
6. Падение одного источника не роняет прогон.
7. Тест на нормализацию с зафиксированной фикстурой в `tests/fixtures/{slug}.json`.
8. Отсутствие ключа или сессии = источник неактивен, а не падение.

## Порядок подключения

| Батч | Фаза | Источники |
| --- | --- | --- |
| Ядро | 3 | `hh`, `telegram`, `jsearch` |
| 1 | 3 | `remotive`, `arbeitnow` |
| 2 | 8 | `adzuna`, `jooble`, `careerjet`, `himalayas`, `remoteok`, `jobicy`, `weworkremotely`, `freehire` |
| 3 | 8 | ATS: `greenhouse`, `lever`, `ashby`, `recruitee`, `workable`, `smartrecruiters`, `personio`, `teamtailor` + список компаний |
| 4 | 8 | `habr`, `getmatch`, `djinni`, `enbek`, `relocate_me`, `landing_jobs` |
| 5 | 8 | `hn_hiring`, `eures`, `cryptojobslist`, `themuse`, `findwork` |
| Импорт | 6 | `linkedin_manual` — эндпоинт ручного импорта |
