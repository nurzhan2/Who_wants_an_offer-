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
5. **Steer** — a workshop screen where the owner sets how their own documents
   are written, without touching code: reference documents to take the *shape*
   of, and checkable rules ("at least 21 items under Skills", "never this
   phrase", "no longer than 2 000 characters"). A hard rule is enforced by
   reading the finished text, not by asking the model; when it cannot be kept
   truthfully, no letter is written and the rule that stopped it is named. Rules
   describe form and never facts: one that would have a document claim
   experience the profile does not list is refused when it is saved.

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
| Frontend | React 18, Vite, TypeScript, TanStack Query, Tailwind |
| Infra | Docker Compose, GitHub Actions CI |

## Quickstart

**Without a terminal:** double-click `start.cmd` in the project folder (Windows;
`uv run python -m wwao up` anywhere). It checks Docker, starts the database,
applies migrations, starts the API and the dashboard unless they already answer,
opens `http://localhost:5173`, and stays in its window as the local agent
watcher. Each step that cannot run says why and what to do. It refuses to reuse
an API on port 8000 started from other code — `/health` carries a digest of the
source it runs — and names the process to stop. An empty `AGENT_API_TOKEN` in
`.env` is filled with a random one. Docker Desktop, `uv` and Node.js 20+ have to
be installed; everything else is started for you.

By hand:

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

## The dashboard

Six screens on `http://localhost:5173`, drawn from one API and from one
database:

| Screen | What it answers |
| --- | --- |
| Обзор | The operations panel — collect vacancies, compute embeddings, rescore, write letters, read outcomes, send what you confirmed — then the corpus, the crawl position, the last runs and the applications counters |
| Вакансии | The ranked list with filters and each posting's age, and one vacancy explained, with «Откликнуться…» |
| Отклики | The applications board: queued, waiting for a person, sent but not yet confirmed by hh, sent — and what hh has since said |
| Документы | Resume upload, resumes with their ATS audit, and every letter with the rules version that judged it |
| Мастерская | Reference documents and checkable rules for generated CVs and letters, with a preview |
| Мои данные | Resume upload, the job titles you search for, and the contact block printed on every CV |

hh's sentence about the resume's visibility, when recorded sends carried it,
hangs under the header on every screen until a newer send comes back without it.

Two rules shape all six.

**Nothing is counted in the browser.** Every number is one SQL statement, so
the screen describes the database rather than the page it managed to fetch.

**`null` is not zero.** A sitemap file no run has counted says «не измерено»; a
vacancy nothing has scored has no score rather than a zero; an application hh
has not answered has no outcome. Each of those is a different fact from the
measured version of itself, and the API, the types and the formatters all keep
them apart.

**The browser confirms; only the local agent sends.** «Откликнуться…» on a
vacancy opens the exact item the agent would be handed — the whole letter, the
score and its reasons, the ATS summary, what hh said before, the posting's age —
and a confirmation there is stored bound to a SHA-256 of that card. It is served
to the agent only while the card and the letter still have those digests and for
`AGENT_CONFIRMATION_TTL_HOURS` (72), and the first result the agent reports
spends it. A vacancy the agent must not touch — archived, filtered, below the
floor, already sent, another source — cannot be confirmed, and the card says
why. «Отправить подтверждённые» asks the local watcher to run
`python -m agent.run --send --dashboard`, which sends only those, re-reads each
vacancy page first, and never sends where hh already counts an application.
The terminal flow, `wwao apply --send`, is unchanged.

**«Отправлено» means hh confirmed it.** A send counts as sent when hh's own
count of applications on the vacancy is at least one or it reported a state for
the conversation; a send the agent reported without that sits in its own column
until «Обновить исходы» reads the page again.

The long operations run as jobs: a button returns at once, its row follows the
operation to the end, and a second copy of a running one is refused. Reading
outcomes and sending need your hh session, so the API only records those two
requests and the watcher started by `start.cmd` (or `python -m wwao watch`)
carries them out.

```bash
uv run python scripts/seed.py    # deterministic data for every screen state
npm --prefix frontend run dev    # proxies /api and /health to :8000
```

## From a crawl to an application

One entry point, six subcommands, in the order you use them:

```bash
uv run python -m wwao crawl                # walk the sources (hh, arbeitnow, remotive, …)
uv run python -m wwao match                # score what was found against the profile
uv run python -m wwao letters --limit 20   # write cover letters: hh first, then the rest
uv run python -m wwao queue                # what is ready to apply to, and why the rest is not
uv run python -m wwao apply                # show each card and send what you confirm
uv run python -m wwao outcomes             # read back what hh says about what you sent
uv run python -m wwao watch                # carry out the dashboard's outcomes and send requests
uv run python -m wwao up                   # start everything, then watch (what start.cmd runs)
```

The first four need no account and no human, which is what makes them the parts
worth running overnight. `queue` is the one to read afterwards: it prints the
reason each vacancy is *not* ready — no letter, an employer test, a closed
posting, an application already sent — because that list is what you would
otherwise reconstruct by hand.

`letters` writes for the agent's source (`AGENT_SOURCE_SLUG`, hh) first: for
those vacancies a letter is what lets them into the agent queue, and without one
the automatic application does not happen. Vacancies listed only on other
sources get letters after them; the dashboard marks them «откликнуться самому»
with a link to the original, next to the CV and letter buttons. `--source agent`,
`--source others` or `--source <slug>` narrows the run, and the report counts
what was written on each side. A filtered vacancy never gets a letter, whatever
its score, and the floor is `agent_queue_min_score`.

`queue` and `apply` both take that list from the backend
(`GET /api/v1/applications/queue`, behind `AGENT_API_TOKEN`): the vacancies
scored above `agent_queue_min_score` that have a letter and no application yet.
`agent/queue.json` is still read, as hand-added rows merged in behind that list
and marked as such — it was the *only* source until 9 September 2026, which is
why a night of crawling, scoring and letter writing used to show up as one row
somebody typed in weeks earlier. A backend that does not answer is reported and
exits non-zero rather than falling back to the file; `--no-backend` (agent) and
`--from <file>` (wwao) ask for the file deliberately.

`apply` is the only subcommand that sends anything, and it needs a person at the
keyboard. It opens **your** hh account in a visible browser window, shows a card
per vacancy — the id, the link, the employer, the match score with its reasoning,
the letter in full, and any warning hh itself raises — and sends only what you
confirm by typing a word. There is no flag that answers that prompt: a closed
stdin, a pipe or a cron job is a refusal, not a default yes. It is a dry run
unless you pass `--send`, and `--send` still only gets you as far as the prompt.

`outcomes` is the third category: it needs the account but not the person. It
opens one page per application you have already sent and writes down what hh
now says about it — `negotiations.total`, and `lastState` if hh has named one —
into `agent/probe/outcomes.json` (that directory is gitignored, and the file
records which jobs you applied to and who turned you down), and into the tracker
as well with `--to http://localhost:8000`. It cannot send anything, and that is structural
rather than a promise: the walk mints no confirmation, so the request gate that
stands in front of every application refuses every application-shaped request
the browser makes, and the run prints how many it refused. It keeps the same
pacing and working hours as `apply` — same account, same session — stops at the
first page hh does not serve as the vacancy asked for, and never changes an
application's recorded state. `lastState` is hh's own vocabulary and is stored
as hh writes it: nothing here decides what a state *means*, and there are two
outcomes on this account so far, which is not enough for any number computed
from them to be worth printing.

The agent is a separate package with a separate risk profile, and the two do not
import each other: `backend/` is anonymous, read-only and safe on a server;
`agent/` acts under your login on your own machine. `wwao` spans both without
fusing them — each subcommand loads only the side it needs, and a test checks
that by looking at `sys.modules` after a real invocation. Details and the first-run
setup are in [agent/README.md](agent/README.md).

## A CV and a letter for one vacancy

Every scored vacancy gets two buttons in the dashboard — «CV под эту вакансию»
and «Сопроводительное» — and both produce a `.docx`, an ATS report beside it,
and a new version that replaces nothing.

**Generating a CV means arranging one, not writing one.** The model reorders the
candidate's skills and jobs so that what the vacancy asks for comes first, picks
which jobs and which of each job's technologies to show, and spells a skill the
way the vacancy spells it when it is the same skill — "PostgreSQL" rather than
"постгрес", because an employer's parser searches for exact strings. It does not
write prose about what somebody did at a job: the profile does not record that,
so anything written there would be invented.

That is why the model returns an *arrangement* — references, orders and names
from a closed list — rather than a document. Company names, job titles, dates,
skill levels and years are read from the database when the file is built, and
there is no field in the model's answer that could carry a different one. So
"the generator does not change dates, companies or titles" is a fact about the
schema rather than a rule somebody checks. What is left to check is checked, in
code, by reading the finished document: a CV for a vacancy requiring Kubernetes,
generated from a profile with no Kubernetes, does not contain the word — however
the model answers, and there is a test that says so.

Every generated document is then audited by this project's own ATS checks,
against the `.docx` that was actually produced rather than the text we meant to
write, and handed over with the report. The report separates «есть, но не
названо в этой версии» — fixable by regenerating — from «нет у кандидата», which
is not fixable and is reported with nothing suggested. A document that fails a
hard rule is not handed over at all; the screen shows what is wrong instead.

The dashboard generates and downloads, and every CV version stays downloadable
from the vacancy card. It never sends: an application goes out from the agent,
under your own account, after you confirmed that card — in the dashboard or at
the keyboard.

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
| Frontend build | `npm --prefix frontend run build` |
| Fast tests (no DB, no model) | `uv run pytest -m "not db and not slow and not network"` |
| Canary against live sources | `uv run pytest -m network` (deselected by default) |
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
