# All commands run from the repository root.
UV ?= uv
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help install install-embeddings queue apply dev test test-fast verify-embeddings embed-backlog parse-resume bench-ollama lint fmt typecheck check migrate revision up down logs seed fixtures

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Sync the virtualenv and install git hooks
	$(UV) sync
	$(UV) run pre-commit install

install-embeddings: ## Add the local embedding model (~1.5 GB of dependencies)
	$(UV) sync --extra embeddings

dev: ## Run the API with autoreload
	$(UV) run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run the test suite with coverage
	$(UV) run pytest

test-fast: ## Tests that need neither PostgreSQL nor a model
	# "not network" is repeated from pyproject's addopts on purpose: a -m on the
	# command line REPLACES that one, so leaving it out points the fast suite at
	# the live hh site.
	$(UV) run pytest -m "not db and not slow and not network"

agent-install: ## Install the apply agent's own dependencies and its browser
	# A group, not a project dependency: backend/Dockerfile builds with
	# `uv sync --frozen --no-dev` and never sees it.
	$(UV) sync --group agent
	$(UV) run playwright install chromium

agent-login: ## Log in to hh by hand, once, in a visible window
	$(UV) run python -m agent.login

agent-probe: ## Stage 0, read-only: make agent-probe u=https://hh.kz/vacancy/123
	$(UV) run python -m agent.probe_apply --stage inspect --url "$(u)"

agent-check: ## The agent's own gates. No browser, no network, no account.
	$(UV) run ruff check agent
	$(UV) run mypy agent
	$(UV) run pytest agent/tests -q

agent-run: ## Dry run by default; add s=--send to reach the confirmation
	$(UV) run python -m agent.run $(s)

queue: ## What is ready to apply to, and why the rest is not
	$(UV) run python -m wwao queue

apply: ## Show each card and send what you confirm. Needs you at the keyboard.
	$(UV) run python -m wwao apply $(s)

verify-embeddings: ## Load the real bge-m3 model and prove it works
	$(UV) run --extra embeddings python scripts/verify_embeddings.py

embed-backlog: ## Compute the vectors the database is missing, without crawling
	# Safe to interrupt: every batch is committed before the next is encoded, so
	# whatever it has written stays written and the next run resumes behind it.
	# Add a="--until-drained" to keep starting passes while one still has work.
	$(UV) run --extra embeddings python scripts/embed_backlog.py $(a)

parse-resume: ## Parse a resume end to end: make parse-resume f=path/to/cv.pdf
	$(UV) run python scripts/parse_resume.py "$(f)"

bench-ollama: ## Measure local inference before phase 3b relies on it
	$(UV) run python scripts/bench_ollama.py

fixtures: ## Regenerate the resume test fixtures
	$(UV) run python scripts/make_resume_fixtures.py

lint: ## Lint and check formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Autoformat and autofix
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Run mypy in strict mode
	$(UV) run mypy backend/app

check: lint typecheck test ## Everything the Definition of Done requires

migrate: ## Apply migrations up to head
	$(UV) run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add vacancy table"
	$(UV) run alembic revision --autogenerate -m "$(m)"

up: ## Start the local stack
	$(COMPOSE) up -d

down: ## Stop the local stack, keep the data volume
	$(COMPOSE) down

logs: ## Tail the stack logs
	$(COMPOSE) logs -f

seed: ## Fill the database with development data
	$(UV) run python scripts/seed.py
