# All commands run from the repository root.
UV ?= uv
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help install install-embeddings dev test test-fast verify-embeddings lint fmt typecheck check migrate revision up down logs seed fixtures

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
	$(UV) run pytest -m "not db and not slow"

verify-embeddings: ## Load the real bge-m3 model and prove it works
	$(UV) run --extra embeddings python scripts/verify_embeddings.py

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
