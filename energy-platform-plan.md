# Energy Data Platform — Repo Structure & Milestone Plan

Portfolio flagship: German day-ahead market data + real PV/battery telemetry (Fenecon Home 10, 8.8 kWp / 14 kWh) → warehouse → dbt marts → battery dispatch optimizer → ML forecasts → dashboard.

**Privacy invariant:** all code, models, and the optimizer are public. Real household telemetry never enters the repo. The repo ships a synthetic sample dataset so `docker compose up` shows the full flow for anyone.

**Engineering invariants (advertise these in the README):**
- Idempotent, re-runnable ingestion (content-hash verification, safe backfills)
- Append-only raw zone; transformations are views/models, never mutations
- No lookahead anywhere: asof joins, time-series CV for ML, forecasts only use data available at prediction time
- NaN over fabrication — missing intervals stay missing and are surfaced by dbt tests

---

## Repo layout

```
energy-platform/
├── README.md                  # architecture diagram, headline savings number, screenshots
├── docker-compose.yml         # dagster + postgres (or duckdb volume) + dashboard
├── pyproject.toml             # uv-managed workspace
├── .github/workflows/ci.yml   # ruff, mypy, pytest, dbt build on sample data
│
├── src/energy_platform/       # typed Python package (the software-engineering showcase)
│   ├── tariffs/               # static / dynamic / feed-in tariff engine
│   ├── dispatch/              # LP battery optimizer (HiGHS via linopy or PuLP)
│   ├── forecasting/           # PV + load forecast models, backtesting harness
│   └── connectors/            # smard/entsoe, open-meteo, openems/home-assistant clients
│
├── orchestration/             # Dagster asset definitions, partitions, schedules
├── dbt/                       # staging → intermediate → marts, tests, docs
│   └── models/
│       ├── staging/           # per-source cleanup, UTC normalization, DST handling
│       ├── intermediate/      # hourly energy balance, price calendar
│       └── marts/             # tariff counterfactuals, dispatch results, forecast eval
│
├── dashboard/                 # Streamlit app (pages: today, economics, optimizer, forecasts)
├── data/sample/               # synthetic telemetry + small real price extract for demo mode
└── tests/                     # pytest incl. property-based tests for invariants
```

---

## Milestones (chunked for plan-then-implement)

### M0 — Scaffold
- uv workspace, ruff + mypy strict, pytest, pre-commit
- CI pipeline green from day one (badge in README)
- docker-compose skeleton, Makefile / justfile with `demo` target
- **Done when:** `just demo` starts an empty but running stack; CI badge is green.

### M1 — Market data ingestion
- SMARD (or ENTSO-E) day-ahead prices + grid load, Dagster partitioned assets
- Append-only raw zone with content hashes; idempotent backfill CLI for full history
- **Done when:** 2+ years of hourly prices load reproducibly; re-running a partition is a no-op.

### M2 — Weather ingestion
- Open-Meteo (or DWD) irradiance + temperature for the site's coordinates (coarse/rounded coords in public config)
- Same raw-zone patterns as M1
- **Done when:** historical + forecast weather lands alongside prices.

### M3 — Telemetry: synthetic first, real second
- Synthetic generator: plausible PV production (from irradiance), household load profile, battery SoC under naive self-consumption — this is what the public demo runs on
- Separate, config-driven connector for real data (Home Assistant REST / OpenEMS Modbus), disabled by default, documented
- **Done when:** demo mode is indistinguishable in shape from real mode; real connector tested locally against the Fenecon.

### M4 — dbt layer (analytics engineering showcase)
- Staging models per source: UTC normalization, DST-switch handling, unit tests on the nasty cases
- Marts: hourly household energy balance joined with prices; data-quality tests (uniqueness, gaps, freshness)
- dbt docs generated + published (GitHub Pages); `dbt build` on sample data in CI
- **Done when:** CI runs dbt with all tests passing; docs site is live.

### M5 — Economics: tariff engine + counterfactuals
- Tariff engine in the package: static tariff, dynamic (day-ahead + margin), feed-in compensation
- Counterfactual mart: cost per month under {static, dynamic} × {battery, no battery}
- "What did the sun earn" metrics: self-consumption rate, autarky, avoided cost vs feed-in revenue
- **Done when:** the dashboard can answer "what would this household have paid under tariff X".

### M6 — Battery dispatch optimizer (the headline)
- LP formulation: minimize cost s.t. battery capacity, charge/discharge power limits, round-trip efficiency, SoC continuity; hourly steps
- Hindsight-optimal dispatch vs actual naive self-consumption → savings in €/year
- Property-based tests: energy conservation, SoC bounds never violated
- **Done when:** README can state "optimal dispatch would have saved €X/year on this system".

### M7 — ML forecasting
- Baselines first: persistence + seasonal-naive for both targets (report these — beating baselines is the credibility test)
- PV forecast: pvlib physical model from irradiance forecast + gradient-boosted residual correction (classy hybrid, shows domain awareness)
- Load forecast: calendar/weather features, gradient boosting
- Backtesting harness with expanding-window time-series CV; strict no-lookahead feature construction; MAE + pinball loss, evaluated in a dbt mart
- **Done when:** both models beat their naive baselines out-of-sample and the eval lives in the warehouse, not a notebook.

### M8 — Forward-looking dispatch ✅
- Run the optimizer on forecasts instead of actuals (rolling daily horizon), then **execute the plan
  against actuals** under an explicit recourse policy, chaining SoC along the executed trajectory
- Report regret: forecast-driven dispatch cost vs hindsight-optimal vs naive, in
  `mart_dispatch_regret`, with regret split into forecast error and day-ahead myopia
- **Done when:** the three-way comparison chart exists — this is the single most impressive figure in the project.
- **Landed:** the chart exists and the headline is *negative* — on synthetic data forecast-driven
  dispatch captures −965% of a €0.29 prize. The decomposition is what makes that a finding rather
  than a failure: a perfect-forecast day-ahead planner captures only 6.7%, so the value is
  structurally out of reach of a daily horizon, not lost to model error.

### M9 — Dashboard + README polish ✅
- Streamlit pages: overview, economics, dispatch comparison, forecast quality — a **pure
  presentation layer**, wired into docker-compose and reading the warehouse through a read-only role
- README: architecture diagram, headline numbers, design-decisions section ("why append-only", "why
  no lookahead"), 60-second demo instructions
- **Done when:** a stranger can `just demo` and see everything within one minute.
- **Landed:** four pages in `dashboard/`, on :8501, organised around the **mart-only rule** — every
  number on screen is a mart column and the app computes nothing. Enforced three ways (a
  `dashboard_ro` role that can read the marts schema and nothing else, an AST guard confining SQL to
  one module, and a per-column contract checked against the built warehouse), each with a positive
  control. The rule did real work rather than describing existing behaviour: it forced two mart
  changes — `mart_coverage_monthly`, because the monthly coverage grain did not exist, and
  `mart_dispatch_regret.attainable_savings_eur`, which the mart had been computing inside a ratio
  and never exposing.
- **Scoped honestly, and the done-when was split rather than fudged.** `just demo` boots to a
  serving dashboard in ~8s on built images (a few minutes on a fresh clone, all of it one
  `docker build`), and it does not load data — seeding plus `just warehouse` is minutes of dbt,
  model fits and MILP. So the dashboard opens in a designed empty state naming the next two
  commands, and the README publishes the three timings separately instead of averaging them into a
  claim that would be false in both directions. That empty path is tested on every push, because it
  is the first thing that happens to a stranger's clone.

### M10 — Plan publisher + thermal telemetry
- **Publisher** (`src/energy_platform/publishing/`): read the day's forward plan out of the
  derived-results layer and publish it to MQTT as a **retained, versioned JSON document** a Home
  Assistant instance can consume — per-hour battery charge/discharge, expected grid exchange, and
  plan metadata (issue time, coverage window, tariff, model/config hashes). The payload is a
  **recommendation, not a command**: this repo never actuates, and the schema says so in a field
  rather than only in prose. Broker host/credentials env-only, disabled by default like the real HA
  connector; retention makes a republished plan an overwrite, never a duplicate stream.
- **Thermal telemetry**: two new channels — indoor temperature and AC power — through the existing
  connector pattern end to end. The synthetic generator gains a deterministic one-zone RC model
  (outdoor temp from M2, configurable R/C, solar gain, a thermostat-driven AC with COP and rated
  power from the shared physics config); the real HA connector gains the same two entities, with
  AC power **null where no separately metered source is configured, never estimated**.
- **Load separability**, the decision M11 depends on: the generator emits `load_base` and
  `ac_power` as separate channels and keeps emitting `household_load` as their sum, so
  `household_load = load_base + ac_power` is a *testable identity* rather than a definition. A
  derived total would be a tautology and could assert nothing; a real total also survives a house
  with no AC meter, where the components are null and the total still stands.
- **Done when:** on the seeded windows the day's plan lands on MQTT topics HA could consume, the
  payload schema is documented well enough to configure HA sensors from it, indoor temperature and
  AC power flow from synthetic generation through staging into the marts, the real-connector
  extension is fixture-tested, and CI is green.

### M11 — Thermal optimization *(placeholder)*
- AC as a flexible load and the house as RC thermal storage: extend the dispatch optimizer to
  schedule cooling against price and PV, measured against M10's thermostat as the "actual behaviour"
  baseline.

---

## Suggested build order note

M0–M4 is a complete, presentable data/analytics-engineering project on its own — pin it once M4 lands, then let M5–M9 accrete. Don't wait for M9 to publish.
