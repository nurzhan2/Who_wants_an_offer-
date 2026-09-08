# Дорожная карта

Каждая фаза — отдельная ветка, отдельный PR, отдельный промт в `prompts/`.
Фаза считается закрытой только по Definition of Done из `CLAUDE.md`.

| # | Фаза | Промт | Результат |
| --- | --- | --- | --- |
| 0 | Каркас репозитория и тулинг | `prompts/01-foundation.md` | `uv`, ruff, mypy, pytest, pre-commit, CI, docker-compose |
| 1 | Ядро бэкенда и БД | `prompts/01-foundation.md` | FastAPI, модели, Alembic, pgvector, health-check |
| 2 | Загрузка и разбор резюме | `prompts/02-ingest.md` | PDF/DOCX → `CandidateProfile` + эмбеддинг |
| 3 | Фреймворк коннекторов + батч 1 | `prompts/02-ingest.md`, `MEGAPROMPT-hh-sitemap.md` | `BaseSource`, реестр, `jsearch`, `remotive`, `arbeitnow`, `hh` (через sitemap) |
| 4 | Нормализация и дедупликация | `prompts/02-ingest.md` | словарь скиллов, парсер зарплат, fingerprint + simhash |
| 5 | Движок соответствия | `prompts/03-matching-api.md` | фильтры, rule-скор, семантика, LLM re-rank |
| 6 | Публичный API | `prompts/03-matching-api.md` | все эндпоинты v1, фильтры, пагинация |
| 7 | Дашборд | `prompts/04-frontend.md` | Шесть экранов: обзор, вакансии с карточкой, доска откликов, документы, мастерская писем, свои данные |
| 8 | Источники, батчи 2–5 | `prompts/05-scale.md` | +15 источников, включая ATS и Telegram |
| 9 | Планировщик и уведомления | `prompts/05-scale.md` | APScheduler, Telegram-алерты, трекер откликов |
| 10 | Аналитика и сопроводительные | `prompts/05-scale.md` | skill-gap, зарплатная аналитика, генератор писем |
| 10a | CV и письмо под вакансию | `prompts/10-cv-per-vacancy.md` | `profile_experience`, `generated_document`, две кнопки, ATS-отчёт на своём выходе |
| 11 | Деплой и наблюдаемость | `prompts/05-scale.md` | Docker, Caddy/SSL, бэкапы, метрики, продовый запуск |

## Порядок работы

```bash
git checkout -b feat/phase-03-sources
# вставить промт фазы в Claude Code
# ревью диффа, прогнать DoD
git commit -m "feat(sources): add BaseSource registry and hh connector"
git push -u origin feat/phase-03-sources
gh pr create --fill
```

Не запускать следующую фазу, пока предыдущая не прошла DoD. Иначе агент
начнёт достраивать на сломанном фундаменте, и разгребать будет дороже.

## Что специально отложено

- Мультипользовательский режим и аутентификация — инструмент личный, v1 на
  одного пользователя. Модели уже завязаны на `profile_id`, поэтому
  расширение не потребует переписывания.
- Автоматическая подача откликов. Технически возможно на hh, но это прямой
  путь к бану аккаунта и мусорным откликам. Генерируем письмо — отправляет
  человек.
- Мобильное приложение. Дашборд адаптивный, этого достаточно.
