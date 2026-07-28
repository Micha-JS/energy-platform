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
    uv run --extra dashboard mypy src tests dashboard

# Run the test suite. The dashboard extra is installed because tests/dashboard renders every page
# with Streamlit's AppTest -- including against an empty warehouse, which needs no data at all.
test:
    uv run --extra dashboard pytest

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
#
# The synthetic vintages stop two days short of each window's end on purpose: a vintage issued on I
# degrades the archive weather for its own 48-hour horizon, so it needs I, I+1 and I+2 ingested, and
# the fixtures end with the window. Target days left without an eligible vintage get no prediction
# rather than a fabricated one -- see forecasting/vintage.py.
dbt-seed:
    uv run energy-platform backfill --offline --from 2024-03-28 --to 2024-06-30 --datasets price,load,weather,telemetry
    uv run energy-platform backfill --offline --from 2024-10-24 --to 2024-10-30 --datasets price,load,weather,telemetry
    uv run energy-platform forecast-snapshot --offline
    uv run energy-platform forecast-backfill --issue-from 2024-03-28 --issue-to 2024-06-28
    uv run energy-platform forecast-backfill --issue-from 2024-10-24 --issue-to 2024-10-28

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

# Backtest the M7 forecast models over every declared coverage window and write the runs and
# predictions to the `derived` schema. Reads mart_hourly_energy AND stg_weather_forecast, so it runs
# after a dbt build and before mart_forecast_eval -- same sandwich as `dispatch`.
#
# OMP_NUM_THREADS=1 is not decoration: HistGradientBoosting accumulates histograms across threads,
# so the float summation order -- and the last bits of every split threshold -- follow the thread
# count. Pinning it is what makes a re-fit on one machine reproduce.
#
# --from/--to backtests an ad-hoc window and reports it WITHOUT writing, exactly as `dispatch` does.
# Example: just forecast --target pv_production_kwh
forecast *ARGS:
    OMP_NUM_THREADS=1 uv run energy-platform forecast {{ARGS}}

# Drop the synthetic forecast vintages so a changed generator can be re-seeded. The raw zone is
# append-only and vintages are keyed on content with a pinned issue_time, so a regenerated vintage
# would otherwise collide with its predecessor on stg_weather_forecast's uniqueness test with no
# way to remove it. Bump GENERATOR_VERSION and run this. Real open_meteo vintages are untouched --
# those genuinely cannot be regenerated.
forecast-reset:
    docker compose exec -T postgres psql -U dagster -d dagster -c \
        "delete from raw.forecast_ingestion where source = 'synthetic'"

# Roll the M8 day-ahead simulation over every declared coverage window: plan each Berlin day from
# the forecasts available at its decision time (D 00:00 Europe/Berlin), execute that plan against
# the actuals, and write the three-way comparison to the `derived` schema. Runs after `forecast`
# and before the regret marts -- same sandwich as `dispatch`, one layer later.
#
# OMP_NUM_THREADS=1 for the same reason `forecast` sets it: this fits models too, one per fold, and
# histogram accumulation is threaded.
#
# --from/--to simulates an ad-hoc window and reports it WITHOUT writing, exactly as the other two do.
# Example: just forward-dispatch --tariff dynamic_2024
forward-dispatch *ARGS:
    OMP_NUM_THREADS=1 uv run energy-platform forward-dispatch {{ARGS}}

# Regenerate the README's three-way comparison figure from the regret marts. Needs a built
# warehouse. matplotlib is a dev dependency, never a runtime one -- see pyproject.
figure *ARGS:
    uv run python scripts/report_regret.py {{ARGS}}

# The full warehouse in dependency order: models, then the three Python steps, then the marts that
# read what they wrote. This is what CI runs, and the only sequence that works on an empty database.
warehouse:
    uv run --project dbt dbt build --project-dir dbt --profiles-dir dbt \
        --exclude mart_dispatch_comparison mart_forecast_eval mart_dispatch_regret \
        mart_forward_dispatch_daily
    uv run energy-platform dispatch
    OMP_NUM_THREADS=1 uv run energy-platform forecast
    OMP_NUM_THREADS=1 uv run energy-platform forward-dispatch
    uv run --project dbt dbt build --project-dir dbt --profiles-dir dbt \
        --select mart_dispatch_comparison mart_forecast_eval mart_dispatch_regret \
        mart_forward_dispatch_daily

# Assert the Python tariff engine and the dbt tariff macro compute the same money, hour by hour,
# over the built warehouse -- plus the no-lookahead manifest guard. Needs `just dbt-build` first;
# ENERGY_REQUIRE_DBT makes a missing warehouse a failure rather than a skip, as CI does.
dbt-reconcile:
    ENERGY_REQUIRE_DBT=1 uv run pytest tests/dbt

# Generate the dbt docs site into dbt/target (index.html + manifest + catalog).
dbt-docs:
    uv run --project dbt dbt docs generate --project-dir dbt --profiles-dir dbt

# Boot the stack (Dagster + Postgres + dashboard) and print where to find it.
#
# Detached, unlike before M9: `up` in the foreground never returns, so it could not print the URLs
# it had just made available. `just demo-logs` is the old behaviour when you want it.
#
# WHAT THIS DOES AND DOES NOT PROMISE. Sixty seconds gets you a running stack with a browsable
# dashboard. It does NOT get you data: seeding the raw zone and building the warehouse is
# `just dbt-seed` then `just warehouse`, which fits forecast models and solves sixty days of MILP
# and takes minutes, not seconds. The dashboard opens in a designed "no data yet" state that names
# both commands, because the alternative -- a stack that boots fast and a first page that is a
# stack trace -- is worse than an honest wait.
demo:
    docker compose up --build -d
    @just dashboard-grants
    @echo ""
    @echo "  Dagster    http://localhost:3000"
    @echo "  Dashboard  http://localhost:8501"
    @echo ""
    @echo "  The warehouse is empty. To fill it:"
    @echo "    just dbt-seed     # offline demo data into the raw zone"
    @echo "    just warehouse    # build every mart"
    @echo ""

# Boot the stack WITH the M10 MQTT broker, and print how to watch what lands on it.
#
# A separate recipe rather than a flag on `just demo`, because the broker is the one part of this
# stack that exists to be published to. Nothing is sent until you run `just publish-plan` with
# ENERGY_MQTT_ENABLED=1 -- the schedule ships stopped and the publisher refuses by default.
demo-broker:
    docker compose --profile broker up --build -d
    @just dashboard-grants
    @echo ""
    @echo "  Dagster    http://localhost:3000"
    @echo "  Dashboard  http://localhost:8501"
    @echo "  Broker     mqtt://localhost:1883  (anonymous, demo only)"
    @echo ""
    @echo "  Watch what a Home Assistant instance would see:"
    @echo "    docker compose exec mosquitto mosquitto_sub -t 'energy/#' -t 'homeassistant/#' -v"
    @echo ""
    @echo "  Then publish a seeded day (run 'just dbt-seed' and 'just warehouse' first):"
    @echo "    ENERGY_MQTT_ENABLED=1 just publish-plan --date 2024-06-12 --tariff dynamic_2024"
    @echo ""

# Publish one Berlin day's dispatch plan to MQTT as a retained recommendation.
#
# Disabled by default: needs ENERGY_MQTT_ENABLED=1 and ENERGY_MQTT_HOST. `--dry-run` prints the
# exact payload without a broker and without writing the publication ledger, which is the way to
# see what a house would receive before enabling anything.
publish-plan *ARGS:
    uv run energy-platform publish-plan {{ARGS}}

# Follow the stack's logs (what `just demo` used to do before it went detached).
demo-logs:
    docker compose logs -f

# Tear the stack down and drop the Postgres volume.
#
# `--profile broker` so a stack started with `just demo-broker` comes down completely. Without it
# the profiled mosquitto container survives `down` and keeps port 1883 -- and its retained plans.
demo-down:
    docker compose --profile broker down -v

# Run the dashboard against a local Postgres, without the container -- the edit-reload dev loop.
# Needs `just dashboard-grants` once, and the dashboard extra: `uv sync --extra dashboard`.
dashboard *ARGS:
    uv run --extra dashboard streamlit run dashboard/app.py {{ARGS}}

# Create the dashboard's read-only role and grant it SELECT on the marts schema -- and nothing
# else. Idempotent, and safe before or after a dbt build (it grants existing tables AND sets
# default privileges for future ones). Compose runs the same file on a fresh volume; this recipe
# is for a database that already existed, or after a rebuild added a mart.
dashboard-grants:
    docker compose exec -T postgres psql -U dagster -d dagster -q -v ON_ERROR_STOP=1 \
        -f - < dashboard/sql/grant_read_only.sql
