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

# Load history into the raw zone (idempotent, resumable). --datasets selects sources.
# Example: just backfill --from 2024-01-01 --datasets price,load
backfill *ARGS:
    uv run energy-platform backfill {{ARGS}}

# Seed the full demo: prices, weather, and synthetic telemetry in dependency order
# (weather before telemetry). Re-running is a content-hash no-op.
# Example: just seed --from 2024-01-01 --to 2024-01-07
seed *ARGS:
    uv run energy-platform backfill --datasets price,load,weather,telemetry {{ARGS}}

# Install the dbt package dependencies (dbt_utils) into dbt/dbt_packages. dbt runs from its own
# isolated uv project (dbt/pyproject.toml) -- its pins conflict with the app's, see dbt/README.
dbt-deps:
    uv run --project dbt dbt deps --project-dir dbt --profiles-dir dbt

# Seed the raw zone offline (recorded fixtures, no network) for the two DST windows the dbt
# layer and its tests are built on, then capture the recorded forecast vintage -- whose issue date
# is derived from the fixture, so it describes the payload it carries. Uses the app CLI (root
# env), not the dbt env.
dbt-seed:
    uv run energy-platform backfill --offline --from 2024-03-28 --to 2024-04-03 --datasets price,load,weather,telemetry
    uv run energy-platform backfill --offline --from 2024-10-24 --to 2024-10-30 --datasets price,load,weather,telemetry
    uv run energy-platform forecast-snapshot --offline

# Build all dbt models and run every test (generic, singular DST, freshness-free).
dbt-build:
    uv run --project dbt dbt build --project-dir dbt --profiles-dir dbt

# Generate the dbt docs site into dbt/target (index.html + manifest + catalog).
dbt-docs:
    uv run --project dbt dbt docs generate --project-dir dbt --profiles-dir dbt

# Boot the empty Dagster + Postgres stack -> http://localhost:3000
demo:
    docker compose up --build

# Tear the stack down and drop the Postgres volume.
demo-down:
    docker compose down -v
