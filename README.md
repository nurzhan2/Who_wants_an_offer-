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
| DB | PostgreSQL 17 + `pgvector` |
| Queue / schedule | APScheduler (v1) → Celery + Redis (v2) |
| Embeddings | `BAAI/bge-m3` via sentence-transformers (multilingual RU/EN/KZ) |
| LLM | Routed per task: Claude Code CLI (subscription), Anthropic API, local Ollama |
| Frontend | React 18, Vite, TypeScript, TanStack Query + Table, Tailwind, shadcn/ui |
| Infra | Docker Compose, GitHub Actions CI |

## Quickstart

```bash
cp .env.example .env             # fill ANTHROPIC_API_KEY at minimum
docker compose up -d db          # PostgreSQL 17 + pgvector, host port 5436
uv sync                          # create .venv from uv.lock
uv run pre-commit install
uv run alembic upgrade head      # from phase 1 on
uv run uvicorn app.main:app --reload   # http://localhost:8000/docs
npm --prefix frontend install && npm --prefix frontend run dev
```

With `make` available, the same thing is `make install && make up && make dev`.

## Parsing a resume

```bash
uv run python scripts/parse_resume.py path/to/cv.pdf
uv run python scripts/parse_resume.py path/to/cv.pdf --show-columns   # no LLM needed
```

Resume extraction is routed to the Claude Code CLI by default, so this needs no
API key — only `claude` on PATH. `--show-columns` prints what pdfplumber makes
of a two-column PDF, which is the fastest way to see why the file goes to the
model whole rather than as extracted text. Names, emails and phone numbers are
masked unless you pass `--show-pii`.

## Development

| Task | Command |
| --- | --- |
| Lint + format check | `uv run ruff check . && uv run ruff format --check .` |
| Autofix | `uv run ruff check --fix . && uv run ruff format .` |
| Type check | `uv run mypy backend/app` |
| Tests + coverage | `uv run pytest` |
| Frontend gates | `npm --prefix frontend run typecheck && npm --prefix frontend run lint` |
| Fast tests (no DB, no model) | `uv run pytest -m "not db and not slow"` |
| Real embedding model | `make verify-embeddings` (needs `uv sync --extra embeddings`) |
| Local inference speed | `make bench-ollama` |

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

Every connector is either a documented API called under its published terms, or
a crawl of the pages `robots.txt` allows — and what that file allows is enforced
in the transport, not left to the connector. LinkedIn, Indeed and Glassdoor are
refused at the transport whatever a connector declares: their terms prohibit
automated collection. HeadHunter is read anonymously through its own sitemap,
never with a query string, because that is the part of the site its `robots.txt`
opens; its search pages and its closed jobseeker API are refused in the same
place. No connector signs in, solves a challenge or disguises its User-Agent,
which identifies the project and carries a contact. Collected data is stored for
personal job-search use only.

## License

MIT
