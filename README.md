# Who wants an offer?

Resume-driven job aggregator. Upload a CV once — the system parses it into a
structured profile, continuously crawls 20+ job sources, scores every vacancy
against the profile and serves the result as a filterable dashboard with an
explainable match score.

> Built for the KZ / RU / EU + remote market. Primary source coverage:
> HeadHunter (hh.kz / hh.ru), Adzuna, Jooble, Greenhouse/Lever/Ashby ATS boards,
> Remotive, Arbeitnow, Himalayas, Habr Career, Telegram job channels.

## What it does

1. **Ingest** — PDF/DOCX/plain-text CV → structured `CandidateProfile`
   (skills with proficiency, seniority, years, domains, languages, location,
   salary expectation, embedding vector).
2. **Collect** — pluggable source connectors run on a schedule, normalize every
   posting into a single `Vacancy` schema, deduplicate cross-posted jobs.
3. **Match** — hybrid scoring: hard filters → weighted skill coverage →
   semantic similarity (bge-m3 embeddings, pgvector) → LLM re-rank of the top N
   with an explanation of *what exactly you are missing*.
4. **Act** — dashboard with filters, application kanban, skill-gap analytics,
   Telegram alerts for high-score hits, AI-generated cover letters.

## Match score

Every vacancy carries a 0–100 score plus a breakdown:

| Bucket | Score | Meaning |
| --- | --- | --- |
| 🟢 Apply now | 85–100 | Meets or exceeds requirements |
| 🔵 Strong | 70–84 | 1–2 non-critical gaps |
| 🟡 Stretch | 55–69 | Reachable, needs preparation |
| ⚪ Skip | < 55 | Not worth the time |

The UI never shows a bare number: it lists matched skills, missing critical
skills, the experience delta and the LLM's verdict. See
[`docs/MATCHING.md`](docs/MATCHING.md).

## Stack

| Layer | Choice |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| DB | PostgreSQL 16 + `pgvector` |
| Queue / schedule | APScheduler (v1) → Celery + Redis (v2) |
| Embeddings | `BAAI/bge-m3` via sentence-transformers (multilingual RU/EN/KZ) |
| LLM | Anthropic Claude (resume extraction, re-rank, cover letters) |
| Frontend | React 18, Vite, TypeScript, TanStack Query + Table, Tailwind, shadcn/ui |
| Infra | Docker Compose, GitHub Actions CI |

## Quickstart

```bash
cp .env.example .env             # fill ANTHROPIC_API_KEY at minimum
docker compose up -d db          # PostgreSQL 16 + pgvector
uv sync                          # create .venv from uv.lock
uv run pre-commit install
uv run alembic upgrade head      # from phase 1 on
uv run uvicorn app.main:app --reload   # http://localhost:8000/docs
npm --prefix frontend install && npm --prefix frontend run dev
```

With `make` available, the same thing is `make install && make up && make dev`.

## Development

| Task | Command |
| --- | --- |
| Lint + format check | `uv run ruff check . && uv run ruff format --check .` |
| Autofix | `uv run ruff check --fix . && uv run ruff format .` |
| Type check | `uv run mypy backend/app` |
| Tests + coverage | `uv run pytest` |
| Frontend gates | `npm --prefix frontend run typecheck && npm --prefix frontend run lint` |

Tests need PostgreSQL. Point `TEST_DATABASE_URL` at a throwaway database
(`docker compose up -d db` provisions `offers_test` automatically). Locally,
database-backed tests skip when no server answers; **in CI a skipped test
fails the build** — green has to mean everything actually ran.

## Docs

- [`CLAUDE.md`](CLAUDE.md) — engineering rules for AI agents working in this repo
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — modules, data flow, schema
- [`docs/SOURCES.md`](docs/SOURCES.md) — source catalogue, endpoints, legality
- [`docs/MATCHING.md`](docs/MATCHING.md) — scoring algorithm specification
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — delivery phases
- [`prompts/`](prompts/) — ready-to-paste Claude Code prompts per phase

## Legal

Only sources with a public API or an explicitly permissive `robots.txt` are
enabled by default. LinkedIn and Indeed connectors are **not** shipped — their
terms of service prohibit automated collection. Every connector respects
`robots.txt`, sets an identifying User-Agent and rate-limits itself.
Collected data is stored for personal job-search use only.

## License

MIT
