# dbt analytics layer

Transforms the append-only raw zone into `staging → intermediate → marts`. See the root
[README](../README.md#dbt-analytics-layer-m4) for the design decisions.

## Isolated toolchain

dbt runs from **its own uv project** (`dbt/pyproject.toml` + `dbt/uv.lock`), not a dependency
group in the root project. dbt-core pins `pathspec<0.13`, which is mutually exclusive with the
app's mypy (needs `pathspec>=1`), so the two cannot share one resolution. Keeping dbt isolated
leaves the app's lock — and its strict type-checking — untouched.

```bash
just dbt-deps      # dbt deps (installs dbt_utils)
just dbt-seed      # offline-seed the raw zone (app CLI, root env)
just dbt-build     # build + test all models (seeds included)
just dbt-reconcile # assert the Python tariff engine matches the SQL macro
just dbt-docs      # generate the docs site into target/
```

Under the hood these are `uv run --project dbt dbt ...` (dbt env) and `uv run energy-platform ...`
(app env). Both talk to the same Postgres; they never share a Python environment.

## Layout

- `models/staging` — one model per source; UTC → Europe/Berlin calendar, consistent units.
- `models/intermediate` — `int_hourly_spine` (declared-coverage UTC grid), `int_hourly_energy`,
  `int_hourly_counterfactual` (per-scenario grid flows), `int_hourly_tariff_cost` (hourly money).
- `models/marts` — `mart_hourly_energy`, `mart_data_quality`, `mart_tariff_counterfactuals`,
  `mart_solar_economics`.
- `seeds/tariffs.csv` — the tariff catalogue. **Also read directly by the Python engine**
  (`src/energy_platform/tariffs/catalog.py`), so it is the single copy of every rate; editing it
  changes both layers at once.
- `tests/` — singular DST tests (23h/25h, no dup/missing UTC hours), counterfactual energy
  conservation, and the partial-month coverage flag.
- `macros/berlin_calendar.sql` — the one place DST calendar columns are derived.
- `macros/tariff_price.sql` — the one place the SQL side of the tariff arithmetic lives.
- `macros/berlin_month_hours.sql`, `macros/declared_coverage_hours.sql` — the calendar length of a
  month and the hours of it the coverage windows claim; shared so both monthly marts count the
  same way.

Two guards live in the app's pytest suite rather than here, because both need artefacts a dbt run
produces (`../tests/dbt/`, run after `dbt build`):

- **no-lookahead** — only `stg_weather_forecast` may read the forecast source, checked against
  `target/manifest.json`.
- **tariff reconciliation** — every priced hour of `int_hourly_tariff_cost` recomputed with the
  Python engine, so the SQL macro and the package cannot drift apart.

Both skip when the warehouse is absent so `just test` stays green without one; `ENERGY_REQUIRE_DBT=1`
(set in CI and by `just dbt-reconcile`) turns that skip into a failure.
