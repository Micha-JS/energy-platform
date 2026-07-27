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
#
# NOTE the ordering around `dispatch`: the optimiser reads mart_hourly_energy and writes the
# `derived` tables that mart_dispatch_comparison reads back, so it sits *between* two dbt runs.
# `just warehouse` does the whole sequence; this recipe alone assumes the derived tables exist
# (they are created empty by `energy-platform dispatch`, so a first-ever build needs `warehouse`).
dbt-build:
    uv run --project dbt dbt build --project-dir dbt --profiles-dir dbt

# Solve the M6 battery dispatch optimiser over every declared coverage window and write the four
# scenarios to the `derived` schema. Needs mart_hourly_energy, so run after a dbt build.
# Re-running replaces a window's rows rather than appending -- see dispatch/store.py for why the
# raw zone's content-hash contract deliberately does not extend here.
#
# --from/--to solves an ad-hoc window and reports it WITHOUT writing: the derived tables hold the
# declared windows and nothing else, which is what lets a solve for any other window be treated as
# stale rather than tolerated. Add the window to coverage_windows in dbt/dbt_project.yml to persist
# it, or --output PATH to keep a scratch copy.
# Example: just dispatch --tariff dynamic_2024
# Example: just dispatch --from 2024-03-29 --to 2024-03-31 --output /tmp/look.json
dispatch *ARGS:
    uv run energy-platform dispatch {{ARGS}}

# The full warehouse in dependency order: models, then the optimiser, then the dispatch mart.
# This is what CI runs, and the only sequence that works on an empty database.
warehouse:
    uv run --project dbt dbt build --project-dir dbt --profiles-dir dbt \
        --exclude mart_dispatch_comparison
    uv run energy-platform dispatch
    uv run --project dbt dbt build --project-dir dbt --profiles-dir dbt \
        --select mart_dispatch_comparison

# Assert the Python tariff engine and the dbt tariff macro compute the same money, hour by hour,
# over the built warehouse -- plus the no-lookahead manifest guard. Needs `just dbt-build` first;
# ENERGY_REQUIRE_DBT makes a missing warehouse a failure rather than a skip, as CI does.
dbt-reconcile:
    ENERGY_REQUIRE_DBT=1 uv run pytest tests/dbt

# Generate the dbt docs site into dbt/target (index.html + manifest + catalog).
dbt-docs:
    uv run --project dbt dbt docs generate --project-dir dbt --profiles-dir dbt

# Boot the empty Dagster + Postgres stack -> http://localhost:3000
demo:
    docker compose up --build

# Tear the stack down and drop the Postgres volume.
demo-down:
    docker compose down -v
