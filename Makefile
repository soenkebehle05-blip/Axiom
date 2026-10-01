PORT ?= 8080
REGION ?= us-east1
ARGS ?=

.PHONY: install setup dev run authorize seed seed-clean scenarios format deploy

install:
	@command -v uv >/dev/null 2>&1 || { echo "-> Installing uv"; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv venv --python 3.13

setup: install
	@echo "-> Installing dependencies"
	uv sync

dev: setup

run:
	uv run uvicorn app.main:app --port $(PORT)

authorize:
	uv run scripts/authorize_google.py

seed:
	uv run scripts/seed_calendar.py

seed-clean:
	uv run scripts/seed_calendar.py --delete

scenarios:
	uv run scripts/run_scenarios.py $(ARGS)

format:
	uvx ruff format .
	uvx ruff check --fix . || true

deploy:
	PROJECT_ID=$(PROJECT_ID) ./scripts/deploy.sh $(REGION)
