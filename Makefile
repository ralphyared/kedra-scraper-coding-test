# Thin wrappers only. Every target is a one-line shim over `uv run` or
# `docker compose`, so nothing in this project depends on make being installed
# -- it is a convenience and a discoverable index of the commands, not a build
# system. Anyone without make can read the recipe and run it directly.
SHELL := /bin/sh
.DEFAULT_GOAL := help

# The demo range defaults to Q1 2024 across all four bodies: 895 records, which
# sits inside the brief's 500-1000 evaluation band.
#   make crawl START=2024-01-01 END=2024-01-31 BODY=wrc
START ?= 2024-01-01
END   ?= 2024-03-31
BODY  ?= all

.PHONY: help sync up down destroy ps logs lint format typecheck test check crawl transform dagster clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed -e 's/:[^#]*## / -- /' | sort

sync:  ## Install or refresh the virtualenv from uv.lock
	uv sync

# `--wait` is applied only to the long-running services. Compose treats ANY
# container exiting as a --wait failure -- including a one-shot job that
# succeeded with code 0 -- so naming them explicitly is what keeps a failed
# `make up` meaningful. The bucket job then runs via `run --rm`, which blocks
# until it finishes and propagates its exit code, so a failed bucket creation
# genuinely fails `make up` instead of being swallowed by -d.
up:  ## Start Mongo + MinIO, create buckets, block until healthy
	docker compose up -d --wait mongo minio mongo-express
	docker compose run --rm mc-init
	@echo "MinIO console : http://localhost:$${MINIO_CONSOLE_PORT:-9001}"
	@echo "mongo-express : http://localhost:$${MONGO_EXPRESS_PORT:-8081}"

down:  ## Stop containers, keep stored data
	docker compose down

destroy:  ## Stop containers AND permanently delete all stored data
	docker compose down --volumes

ps:  ## Show container status
	docker compose ps

logs:  ## Follow container logs
	docker compose logs -f

lint:  ## Check lint rules and formatting
	uv run ruff check .
	uv run ruff format --check .

format:  ## Apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck:  ## Run mypy
	uv run mypy

test:  ## Run the offline unit tests
	uv run pytest

check: lint typecheck test  ## Run every check, as CI would

crawl:  ## Crawl a date range. Vars: START, END, BODY
	uv run kedra crawl --start $(START) --end $(END) --body $(BODY)

transform:  ## Transform landing into curated. Vars: START, END
	uv run kedra transform --start $(START) --end $(END)

dagster:  ## Launch the Dagster UI
	uv run dagster dev -m orchestration.definitions

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
