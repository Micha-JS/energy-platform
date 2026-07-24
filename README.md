# Energy Data Platform

German day-ahead market data + real PV/battery telemetry (Fenecon Home 10, 8.8 kWp /
14 kWh) → warehouse → dbt marts → battery dispatch optimizer → ML forecasts → dashboard.

> **Status: M0 — Scaffold.** This is the empty-but-running foundation: tooling, CI, and a
> bootable Dagster + Postgres stack. Ingestion, dbt models, the optimizer, forecasting, and
> the dashboard land in later milestones.

## Architecture

```
                       ┌─────────────┐
  market prices ──▶    │             │
  weather      ──▶     │  Dagster    │ ──▶  raw zone  ──▶  dbt  ──▶  marts
  PV/battery   ──▶     │ (orchestr.) │      (Postgres)   staging    (economics,
  telemetry            │             │                   → marts     dispatch,
                       └─────────────┘                               forecasts)
                                                             │
                                        optimizer + forecasts│──▶ Streamlit
                                        (energy_platform pkg) ▼    dashboard
```

- **`src/energy_platform/`** — typed Python package: `tariffs/`, `dispatch/` (LP battery
  optimizer), `forecasting/` (PV + load models), `connectors/` (market/weather/telemetry
  clients). The Dagster code location lives at `energy_platform.definitions:defs`.
- **Orchestration** — Dagster assets, partitions, and schedules, backed by Postgres for run
  and event storage. (Assets migrate into `energy_platform.orchestration` at M1.)
- **Warehouse & transforms** — append-only raw zone; dbt staging → intermediate → marts.

## Engineering invariants

- **Idempotent, re-runnable ingestion** — content-hash verification, safe backfills.
- **Append-only raw zone** — transformations are views/models, never mutations.
- **No lookahead** — asof joins, time-series CV, forecasts use only data available at
  prediction time.
- **NaN over fabrication** — missing intervals stay missing and are surfaced by dbt tests.
- **Reproducible builds** — `uv.lock` committed; CI and the Docker image install `--frozen`.

## Privacy

All code, models, and the optimizer are public. Real household telemetry never enters the
repo — a synthetic sample dataset ships so `just demo` shows the full flow for anyone.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), [just](https://github.com/casey/just), and Docker.

```bash
just install     # install locked dependencies
just check       # lint + strict type-check + tests
just demo        # boot Dagster + Postgres -> http://localhost:3000
just demo-down   # stop and drop the Postgres volume
```

## Development

| Command          | Purpose                                        |
| ---------------- | ---------------------------------------------- |
| `just lint`      | ruff lint + format check                       |
| `just fmt`       | auto-fix and format                            |
| `just typecheck` | mypy (strict)                                  |
| `just test`      | pytest                                         |
| `just lock`      | regenerate `uv.lock` after changing deps       |

CI runs the full quality gate plus `docker compose config` validation on every push and PR.

## License

[MIT](LICENSE)
