set shell := ["bash", "-uc"]

# List available recipes.
default:
    @just --list

# Install locked dependencies (idempotent).
install:
    uv sync --frozen

# Regenerate the lockfile after changing dependencies.
lock:
    uv lock

# Lint + format check (no writes).
lint:
    uv run ruff check .
    uv run ruff format --check .

# Auto-fix lint issues and format.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Strict type checking.
typecheck:
    uv run mypy src tests

# Run the test suite.
test:
    uv run pytest

# Full local gate — mirrors CI.
check: lint typecheck test

# Load SMARD market-data history into the raw zone (idempotent, resumable).
# Example: just backfill --from 2024-01-01
backfill *ARGS:
    uv run energy-platform backfill {{ARGS}}

# Boot the empty Dagster + Postgres stack -> http://localhost:3000
demo:
    docker compose up --build

# Tear the stack down and drop the Postgres volume.
demo-down:
    docker compose down -v
