# dbt analytics layer

Transforms the append-only raw zone into `staging → intermediate → marts`. See the root
[README](../README.md#dbt-analytics-layer-m4) for the design decisions.

## Isolated toolchain

dbt runs from **its own uv project** (`dbt/pyproject.toml` + `dbt/uv.lock`), not a dependency
group in the root project. dbt-core pins `pathspec<0.13`, which is mutually exclusive with the
app's mypy (needs `pathspec>=1`), so the two cannot share one resolution. Keeping dbt isolated
leaves the app's lock — and its strict type-checking — untouched.

```bash
just dbt-deps     # dbt deps (installs dbt_utils)
just dbt-seed     # offline-seed the raw zone (app CLI, root env)
just dbt-build    # build + test all models
just dbt-docs     # generate the docs site into target/
```

Under the hood these are `uv run --project dbt dbt ...` (dbt env) and `uv run energy-platform ...`
(app env). Both talk to the same Postgres; they never share a Python environment.

## Layout

- `models/staging` — one model per source; UTC → Europe/Berlin calendar, consistent units.
- `models/intermediate` — `int_hourly_spine` (declared-coverage UTC grid) + `int_hourly_energy`.
- `models/marts` — `mart_hourly_energy`, `mart_data_quality`.
- `tests/` — singular DST tests (23h/25h, no dup/missing UTC hours).
- `macros/berlin_calendar.sql` — the one place DST calendar columns are derived.

The no-lookahead guard (only `stg_weather_forecast` may read the forecast source) lives in the
app's pytest suite at `../tests/dbt/test_no_lookahead.py`, run against `target/manifest.json`.
