# Energy Data Platform

[![CI](https://github.com/Micha-JS/energy-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/Micha-JS/energy-platform/actions/workflows/ci.yml)
[![dbt docs](https://img.shields.io/badge/dbt%20docs-live-ff694b)](https://micha-js.github.io/energy-platform/)

German day-ahead market data + real PV/battery telemetry (Fenecon Home 10, 8.8 kWp /
14 kWh) → warehouse → dbt marts → battery dispatch optimizer → ML forecasts → dashboard.

![Forward-looking dispatch: naive self-consumption vs forecast-driven dispatch vs the hindsight
optimum, over 60 rolling days of the synthetic demo data](docs/img/regret_three_way.png)

> **Status: M9 — dashboard.** A Streamlit app in `dashboard/`, wired into `docker compose`, with
> four pages: the hourly energy balance and coverage, the tariff and solar economics, the three-way
> dispatch comparison, and the forecast evaluation. Its organising constraint is the **mart-only
> rule**: every number on screen is a column of a dbt mart, and the app computes nothing. That is
> enforced three ways rather than asserted — the dashboard connects as a `dashboard_ro` role that
> can read the marts schema and nothing else, an AST guard keeps SQL out of every module but the
> query layer, and each query declares its columns as data so CI can check them against the
> warehouse. Two marts were added *because* of the rule, not around it. Details in
> [Dashboard (M9)](#dashboard-m9). Previously:
>
> **M8 — forward-looking dispatch.** The milestone where the forecasts meet the optimizer:
> each Berlin day's battery schedule is planned at that day's decision time from the forecasts
> available then, and then *executed against what actually happened*. `mart_dispatch_regret` sets the
> result beside the naive baseline and the hindsight optimum, and the honest answer on synthetic data
> is that **forecast-driven dispatch captured −965% of the available savings** — it lost money. That
> is not a bug and it is the most interesting number in the repo: the prize is **€0.29** over sixty
> days, and acting on forecasts good enough to score 0.18 kWh MAE costs **€2.86**. A day-ahead
> planner given *perfect* forecasts captures only **6.7%**, so almost none of the shortfall is
> forecasting's fault — the value simply is not reachable one day at a time. Details in
> [Forward-looking dispatch (M8)](#forward-looking-dispatch-m8). Previously:
>
> **M7 — ML forecasting.** Day-ahead hourly PV and load forecasts, evaluated against
> persistence and seasonal-naive baselines in `mart_forecast_eval`. The deliverable is the
> *harness*, not an accuracy number: a backtester that provably rejects a leaked feature (four
> deliberate cheating attempts, plus a positive control), one vintage-selection rule stated once
> and enforced everywhere, and an observation lag that makes "persistence = yesterday" the lookahead
> it actually is on this platform. On seeded synthetic data the flat-plate PV model is the *oracle* —
> it generated the labels — so the mart labels it `role='oracle'` rather than letting it be ranked.
> Details in [ML forecasting (M7)](#ml-forecasting-m7).
>
> **M6 — the dispatch optimizer.** A MILP (HiGHS via PuLP) computes the
> *hindsight-optimal* battery schedule for every covered week under each tariff, using the same
> `BatteryConfig` the synthetic generator simulates and the same tariff engine the economics marts
> price with — so `mart_dispatch_comparison` compares four dispatches on identical physics and
> identical money. Over the two covered weeks the battery is worth **€20–22**, and optimal dispatch
> adds **€0.36** on top of naive self-consumption. That second number being small is the finding,
> not a disappointment: see [the headline, honestly](#the-headline-honestly). Property tests assert
> energy conservation, SoC bounds, and both exclusivities on every solution, and prove the optimum
> never costs more than a baseline that is feasible for its own problem. CI rebuilds the warehouse,
> solves, and rebuilds on offline-seeded data, and publishes the
> [dbt docs site](https://micha-js.github.io/energy-platform/).

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

- **`src/energy_platform/`** — typed Python package: `tariffs/`, `dispatch/` (MILP battery
  optimizer, HiGHS via PuLP), `forecasting/` (PV + load models), `connectors/`
  (market/weather/telemetry clients). The Dagster code location lives at
  `energy_platform.definitions:defs`.
- **Orchestration** — Dagster assets, partitions, and schedules in
  `energy_platform.orchestration`, backed by Postgres for run and event storage.
- **Warehouse & transforms** — append-only raw zone; dbt staging → intermediate → marts.
- **`dashboard/`** — the Streamlit presentation layer, outside the package on purpose: it reads
  marts through a read-only role and computes nothing, and the scientific and plotting stacks are
  fenced out of `src/` (see [Dashboard (M9)](#dashboard-m9)).

## Market data (M1)

The SMARD connector fetches the German day-ahead wholesale price and grid load. No API key
is required, so anyone cloning the repo can load real history immediately.

```bash
docker compose up -d postgres          # the raw zone reuses the stack's Postgres
just backfill --from 2024-01-01        # load history (idempotent, resumable)
just backfill --from 2024-01-01        # re-run: every partition is a content-hash no-op
```

Design notes worth calling out:

- **Daily partitions in `Europe/Berlin`** — the market's own calendar, so DST-transition
  days correctly span **23 or 25 hours** (verified in tests on 2024-03-31 and 2024-10-27).
- **Raw zone** (schema `raw`) — an append-only `smard_ingestion` ledger stores each day's
  series exactly as received (JSONB) with a sha256 content hash, alongside a long-form
  `observations` table (`ts_utc`, `value`, `is_missing`). The `observations_current` view
  resolves the latest version per partition, so a SMARD revision appends a new version
  rather than mutating history.
- **One idempotent core, two entry points** — the CLI and the Dagster assets both call the
  same `ingest_partition`, so a Dagster backfill over a CLI-loaded range shows green
  partitions and is a proven no-op (matching hashes).
- **Missing stays missing** — a value SMARD reports as `null` becomes `is_missing = true`;
  absent intervals are surfaced by `row_count < expected_count`, never interpolated.

The connector is unit-tested entirely against recorded fixtures (`tests/connectors/fixtures/`,
regenerated by `scripts/record_smard_fixtures.py`) — CI makes no live API calls. Adding a
second source (e.g. ENTSO-E) means implementing the `MarketDataConnector` protocol; nothing
downstream changes.

## Weather (M2)

[Open-Meteo](https://open-meteo.com) supplies weather for the site — shortwave / direct /
diffuse irradiance, 2 m temperature, cloud cover, and 10 m wind speed, hourly in UTC. No API
key is required. Two **distinct truths** land separately and are never merged at this layer:

- **Historical actuals** (archive API, ERA5-backed) — the weather that happened. Fully
  backfillable and structurally identical to market data, so they reuse the M1
  `ingestion` / `observations` tables and the same `ingest_partition` core (source
  `open_meteo`), one series per variable.
- **Forecasts** (forecast API) — what was predicted, captured **daily as immutable vintages**
  in dedicated `forecast_ingestion` / `forecast_observations` tables, each stamped with its
  `issue_date` / `issue_time`.

```bash
just backfill --from 2024-01-01 --datasets weather   # actuals, alongside prices
uv run energy-platform forecast-snapshot             # capture today's forecast vintage
```

Design decisions worth calling out:

- **Forecast vintages, never "latest".** A later forecast is a *new independent truth*, not a
  correction of the old one — so vintages accumulate and are never collapsed to a single
  series. Keeping only the latest forecast would turn every backtest into silent lookahead
  (scoring a decision against data that didn't exist when it was made). Forecasts can only
  accrue **forward** — the API serves the current issue, so past vintages can't be
  backfilled — which is exactly why capturing issue-timestamped snapshots from day one
  matters: by the time the forecasting and forward-dispatch milestones (M7–M8) need it, there
  are months of honestly-accumulated history to backtest against.
- **Actuals and forecasts are separate truths.** Separate raw tables, never merged here —
  different provenance, different backfillability, and a different query grain (`(target_ts)`
  for actuals vs `(issue_date, target_ts)` for forecasts).
- **Coarse public coordinates.** The site's coordinates live **only** in config, rounded to
  ~2 dp (~1 km) — accurate enough for weather, deliberately vague about an address. No precise
  copy exists anywhere in the repo.
- **UTC end to end.** Values are requested from Open-Meteo in UTC directly; partitions stay
  Europe/Berlin calendar days so weather aligns with the market's DST-correct day boundaries.

Both weather connectors are unit-tested against offline fixtures
(regenerated by `scripts/record_open_meteo_fixtures.py`) — CI makes no live API calls.

## Telemetry (M3)

Household telemetry — PV production, load, battery charge/discharge, state of charge, and grid
import/export — comes from two producers that land in the **identical** raw-zone schema, so
nothing downstream knows or cares which ran:

- **Synthetic generator** (source `synthetic`) — what the public demo and CI run on.
  **Synthetic telemetry is a pure function of `(config, date)`** and the day's ingested
  irradiance: PV from a simple GHI × performance-ratio model with a temperature derate, a
  plausible daily/weekly/seasonal load profile with seeded noise, and a battery simulated under
  **naive self-consumption**. Because it's deterministic, re-ingestion is a content-hash no-op
  exactly like a real source — and no real data ever touches the repo.
- **Real connector** (source `fenecon`) — a **read-only**, config-driven Home Assistant client
  for the actual Fenecon Home 10, **disabled by default** (credentials only via env, never
  committed, no live calls in CI). OpenEMS Modbus TCP is a documented alternative backend behind
  the same connector protocol.

```bash
just seed --from 2024-01-01 --to 2024-01-07   # prices + weather + telemetry, in dependency order
```

Design decisions worth calling out:

- **Determinism is the data contract.** The per-day RNG seed is `sha256("{salt}:{date}")`, all
  values are quantised to Wh at one emission boundary, and generation is stdlib-only — so hashes
  are identical across machines and idempotency holds for the one component whose whole job is
  reproducibility.
- **The naive battery model is load-bearing beyond M3.** It becomes M6's "actual behaviour"
  baseline, so its efficiency and power limits live in the same `BatteryConfig` the optimizer
  will read — same physics on both sides of the savings comparison.
- **Energy conservation is property-tested.** The AC-node balance closes every hour and SoC
  never leaves its bounds, checked with Hypothesis; missing irradiance yields null PV (NaN over
  fabrication), never a fabricated value.

The connectors are unit-tested against offline fixtures / stubs — CI makes no live calls and
never reaches a live house.

## dbt analytics layer (M4)

The [`dbt/`](dbt/) project (dbt-postgres) transforms the raw zone into an analytics warehouse:

- **Staging** — one model per source (`stg_prices`, `stg_grid_load`, `stg_weather_actuals`,
  `stg_telemetry`, `stg_weather_forecast`). Reads the latest-wins `observations_current` view,
  renames to typed unit columns, and derives Europe/Berlin calendar columns from the UTC
  instants — this is the single place DST semantics live. Every staging grain carries `source`, so
  two connectors writing the same series stay separate truths instead of blending.
- **Intermediate** — `int_hourly_spine` (a UTC-hour grid) joined to household energy balance and
  price in `int_hourly_energy`; an hour missing in any source stays **null**, never filled.
- **Marts** — `mart_hourly_energy` (energy balance + price for every hour, the foundation M5's
  economics and M6's optimizer both build on), `mart_data_quality` (per-source gaps, nulls, and
  live freshness) and, from M9, `mart_coverage_monthly` (the same coverage question at monthly
  grain, added because the dashboard may not aggregate one itself).

```bash
just dbt-deps     # install dbt_utils
just dbt-seed     # offline-seed the raw zone (recorded fixtures, no network)
just dbt-build    # build all models + run every test
just dbt-docs     # generate the docs site into dbt/target
```

Design decisions worth calling out:

- **DST lives in one layer, tested on the real dates.** Local calendar columns come from
  `ts_utc AT TIME ZONE 'Europe/Berlin'`; the spine is generated in absolute UTC, so the
  spring-forward and fall-back days resolve to **23** and **25** rows — asserted by singular tests
  on 2024-03-31 and 2024-10-27, alongside no-duplicate / no-missing UTC-hour checks. Uniqueness is
  always keyed on `ts_utc`, never local columns (the fall-back day repeats local 02:00). Those
  assertions are unconditional: expected hour counts come from the declared coverage windows and
  the Berlin calendar, never from the rows under test, so a missing day or an empty mart fails
  rather than passing by silence.
- **The spine is declared, not inferred.** Coverage windows are a dbt `var`, so the two disjoint
  seeded windows never manifest a phantom void between them — a gap means "missing *within
  declared coverage*".
- **One connector per site, chosen explicitly.** The synthetic demo generator and the real Fenecon
  reader can both write the same site; the energy mart is one row per hour per site, so a
  `telemetry_source` var selects between them. There is no default — a wrong guess would either
  drop the real house or empty the demo — so with two sources present and no choice made, a named
  singular test fails and says which var to set.
- **No lookahead, enforced structurally.** `stg_weather_forecast` is the only model allowed to read
  the forecast vintages, and it carries the `(issue_time, target_time)` dimension forward. A pytest
  over the compiled dbt manifest fails the build if any other model selects from the forecast
  source — the M2 no-lookahead promise, machine-checked.
- **CI rebuilds the warehouse from the real pipeline.** The shipped `energy-platform backfill
  --offline` seeds the raw zone from recorded fixtures (deterministic, zero network) across both
  DST windows, a re-seed proves idempotency on every PR, then `dbt build` runs all tests and the
  docs site publishes to GitHub Pages. The offline forecast vintage takes its issue date from the
  recorded fixture rather than a pinned literal, so it always describes the payload it carries —
  and ingestion rejects any vintage reaching past the horizon it claims.

## Economics: tariff engine + counterfactuals (M5)

What the household would have paid. A typed tariff engine in
[`src/energy_platform/tariffs/`](src/energy_platform/tariffs/) prices energy — **static** (flat
ct/kWh + monthly Grundpreis), **dynamic** (hourly day-ahead spot + supplier margin + fixed
pass-through), and **feed-in** (flat statutory compensation per exported kWh) — and two marts turn
that into monthly answers:

- **`mart_tariff_counterfactuals`** — cost per site per month under {static, dynamic} ×
  {battery, no battery}, plus feed-in revenue and a net figure.
- **`mart_solar_economics`** — self-consumption rate, autarky rate, and what a PV kWh was worth,
  split between grid cost avoided and feed-in revenue.

```bash
just dbt-build       # seeds the tariff catalogue, builds the economics marts, runs every test
just dbt-reconcile   # assert the Python engine and the SQL macro compute the same money
```

Design decisions worth calling out:

- **One CSV, two readers — parameters cannot drift.** M6's optimizer needs tariff logic in Python
  (it evaluates cost inside an LP objective and cannot call SQL per candidate dispatch); the marts
  need cost columns in SQL. Rather than render one from the other,
  [`dbt/seeds/tariffs.csv`](dbt/seeds/tariffs.csv) is the **only** place any rate is written down:
  dbt loads it as a seed, and `energy_platform.tariffs.catalog` reads the same file. There is no
  generated artefact, so there is no staleness bug class.
- **Arithmetic is written twice, and pinned by a test.** Two runtimes means two implementations —
  that part is unavoidable. `tests/dbt/test_tariff_reconciliation.py` recomputes **every priced
  hour** the warehouse built with the Python engine and asserts equality, so a factor-of-ten unit
  slip, VAT applied on one side only, or a sign error fails CI naming the hour and tariff that
  diverged. It is the guard, not the documentation, that keeps the two honest.
- **EUR/MWh → ct/kWh is a factor of ten.** The warehouse stores wholesale price in EUR/MWh; retail
  tariffs are quoted in ct/kWh. A `/1000` is silent — the numbers stay plausible, just an order of
  magnitude too small — so the conversion is a named constant on both sides and asserted explicitly
  by a dbt unit test *and* the reconciliation.
- **`greatest(NULL, 0)` returns 0 in Postgres.** The no-battery counterfactual is
  `import = max(load − pv, 0)`, and the obvious one-liner turns a gapped hour into a fabricated
  zero-import hour — "we don't know" quietly becoming "it cost nothing". Every derived flow carries
  an explicit null guard, with a named regression test that fails against the naive form.
- **Negative prices are in contract, and the two sides of the meter differ.** Day-ahead prices go
  negative and nothing clamps them: a dynamic import price below zero means the household is paid to
  consume. Feed-in is a flat statutory rate that does *not* track the market, so a negative hour
  still earns full compensation — computing it as `spot × export` would be a sign bug that a
  negative hour makes expensive. Both directions are unit-tested.
- **Money is exact decimal, cast to float at the boundary.** Computed in floating point, the price
  stack returns 13.316099999999997 where 13.3161 was meant, and that noise accumulates through every
  monthly sum. The arithmetic runs in `numeric`; the column type stays `double precision`.
- **Partial months are flagged, never presented as whole ones.** Since M7 extended the seeded
  coverage to a contiguous spring quarter, April and May *are* complete (720/720 and 744/744) while
  March, June and October are not — so the pro-rating finally exercises both branches rather than
  only the partial one. June is the interesting case: it is fully *covered* at 720/720 and still
  flagged partial, because one hour is priced 719/720. That hour is the hand-injected `null` in the
  curated `2024-06-12` fixture, which the extended window swallowed — a real gap, surfaced rather
  than smoothed, exactly as intended. Every monthly row carries `expected_hours`
  (the DST-correct calendar length — 743 in March, 745 in October, derived from the calendar and
  never counted from the rows under test), `covered_hours`, `priced_hours`, `completeness_ratio`,
  and `is_partial_month`. The Grundpreis is pro-rated to match, with the full monthly fee kept
  alongside so that can be undone.
- **The demo's battery looks worse than the hardware is, and the mart says so.** The M3 generator
  simulates each Berlin day independently — that is what makes it a pure function of
  `(config, date)` — so state of charge resets at midnight and stored energy is discarded. Over the
  seeded March window the battery charges 54 kWh and discharges 13. Rather than quietly present a
  misleading comparison, both marts expose `battery_unreturned_kwh`. Carrying SoC across partitions
  is an M3 change (it rewrites every content hash) and is tracked separately.

## Battery dispatch optimizer (M6)

The headline. A mixed-integer linear program in
[`src/energy_platform/dispatch/`](src/energy_platform/dispatch/) computes the **hindsight-optimal**
battery schedule for each declared coverage window under each tariff — minimise cost subject to
capacity, separate charge/discharge power limits, round-trip efficiency, SoC continuity and SoC
bounds, in hourly steps — and `mart_dispatch_comparison` sets it against three baselines.

The comparison is like-for-like **by construction, not by assertion**: the physics is the same
`BatteryConfig` the M3 generator simulates under, the money is the same `energy_platform.tariffs`
engine the M5 marts price with, and all four scenarios are settled by one function from one set of
hourly inputs.

| Scenario | What it is |
| --- | --- |
| `no_battery` | PV only — M5's counterfactual |
| `naive_telemetered` | exactly what the M3 generator emitted |
| `naive_continuous` | the same naive policy, SoC carried across the window |
| `optimal` | the MILP |

```bash
just warehouse    # dbt build -> the Python steps -> dbt build (the only order that works)
just dispatch     # re-solve on an already-built warehouse
```

### The headline, honestly

Over the two seeded DST weeks (167 + 169 hours — so not 336). M7 added a third, contiguous spring
window (2024-04-04..06-30) deliberately *without* disturbing these two, so every number below is
unchanged; over that window the battery is worth a further €71 and optimal dispatch adds €0.28 on
top of naive:

| | over the covered weeks | very rough annualisation |
| --- | --- | --- |
| Battery vs no battery, naive dispatch | **€20.24** (dynamic) / **€21.80** (static) | ~€530 / ~€570 |
| Optimal dispatch on top of naive | **€0.36** (dynamic) / **€0.00** (static) | ~€9 / ~€0 |

**The annualisation is an extrapolation, not a measurement, and a bad one.** Two weeks is 4% of a
year, both windows are shoulder-season, and the March week is unusually sunny while the October week
is dark — the same arithmetic applied to the absolute costs produces a household that *earns* money
every year, which is obviously false. The per-window figures are what the data supports. Once real
Fenecon telemetry has accumulated a few months, this table gets replaced with a measured number.

Design decisions worth calling out:

- **The optimizer is worth much less than the battery, and that is the interesting result.** Naive
  self-consumption already captures nearly all of the value: this battery is sized for
  self-consumption, so in a sunny week it is saturated by PV with no headroom left to arbitrage, and
  optimal dispatch is *identical* to naive. The €0.36 comes almost entirely from the darker October
  week, where there is spare capacity and a dynamic price to exploit. Reporting the small number
  next to the large one is the whole point — a "€500/year optimizer" claim would be attributing the
  battery's value to the optimizer.
- **Which baseline you pick changes the answer by 50×.** Measured against `naive_telemetered`, the
  optimizer "saves" €16.92 over the two weeks — about €440/year. Almost all of that is M3's midnight
  SoC reset: the generator discards stored energy at every day boundary, so that baseline pays for
  energy it never uses (69.6 kWh unreturned in the March window against 14.8 for continuous SoC).
  `naive_continuous` runs the *identical* `dispatch_hour` function through the *identical* config and
  differs only in carrying SoC through midnight, which is why it is the baseline and why
  `naive_telemetered` is reported beside it rather than quietly dropped.
- **Negative prices break a pure LP, and the meter is the live trap — not the battery.** The
  briefing for this milestone expected simultaneous charge/discharge to be the problem: an LP paid
  to consume will burn energy through the round-trip losses. Working it through, that turns out to
  be *weakly dominated* once the meter is exclusive — raising both legs together moves the metered
  flow not at all and strictly costs SoC — so no price makes it strictly profitable. The real
  failure is one step earlier. Feed-in is a **flat** statutory rate, so as soon as the retail import
  price drops below it, importing and exporting the same kWh is paid at both ends and the LP is
  **unbounded**. With this catalogue that crossover is **−123.75 €/MWh**, not the deep tail; no
  battery is involved, so no battery-side post-check would ever have caught it, and there is no
  optimum to repair towards. `tests/dispatch/test_negative_prices.py` derives the threshold from the
  seed and shows the naive LP bounded five euros above it and unbounded five below.
- **So: MILP, and the two binaries are justified differently.** Meter exclusivity is load-bearing.
  Battery exclusivity is a *guarantee against degeneracy* — the test asserts that dropping it alone
  leaves the optimal value unchanged, which is the honest claim. Optimal solutions are non-unique,
  and which vertex a simplex returns is not something a model gets to promise; the binary turns "the
  solver happens not to" into "the formulation does not permit it", for ~336 binaries on a week —
  or ~4 200 on M7's 88-day window, which HiGHS still closes in about a second. Big-Ms come from each hour's own PV and load, and are attached to the exclusivity
  constraints only — putting them on the variable bounds as well would have kept the relaxation
  finite and hidden the pathology.
- **`SoC_end ≥ SoC_start` is a trap: it is vacuous.** Without any boundary condition, hindsight
  optimization drains the battery on the last evening and books the proceeds as savings. The obvious
  fix does nothing, because the window starts at `soc_min` and the SoC lower bound already forces
  it. Terminal energy is **valued in the objective** instead, at the cheapest non-negative import
  price the window offered. The mean is the tempting choice and is wrong: charging at `p` and being
  credited `λ·√rte` pays whenever `p < λ·rte`, which for a mean is most hours — on a test day the
  optimizer hoarded 14.5 kWh in and 2.0 out, finishing 88% full. The minimum makes hoarding provably
  unprofitable while still stopping the drain. Every row carries its terminal SoC delta and credit,
  so the adjustment can be recomputed at any other valuation; sweeping it from 0 to 40 ct/kWh moves
  the optimizer's saving only between €0.00 and €0.89.
- **Valuing it that way is what makes "optimal ≤ naive" a theorem.** `naive_continuous` and
  `no_battery` satisfy every constraint the optimizer solves under and start from the same SoC, so
  both are *feasible points of its own problem* and a minimum cannot exceed them. A cyclic
  `SoC_end = SoC_start` constraint would have pushed both out of the feasible set and left the
  property test asserting something that could legitimately fail. `naive_telemetered` is not
  feasible — its SoC jumps at midnight — and is deliberately excluded from that assertion.
- **The optimality bound is stated in money, not as a fraction — and M7 is how we found out.**
  HiGHS defaults to a *relative* MIP gap of 1e-4, which is a slack budget that scales with the
  objective. At one-week windows the objective was ~€-9 and that bought ~0.9 mEUR, invisible. When
  M7's 88-day window pushed it to ~€-194, the same fraction bought ~19 mEUR, and HiGHS stopped —
  entirely correctly — at a schedule costing 2.8 mEUR *more* than `naive_continuous`, which
  `assert_optimal_never_costs_more_than_naive` duly reported as a violated theorem. The gap is now
  absolute (1e-7 EUR, below settlement's own rounding) with the relative gap pinned to zero. This
  was a latent M6 defect rather than an M7 one: it was always wrong to let the optimality bound
  scale with the window, and a longer window is simply what made it visible.
- **Ingestion is bit-reproducible; optimization results are not, and the repo says so.** This is the
  first component the raw zone's content-hash contract deliberately does not cover. The optimal
  *value* is unique; the optimal *schedule* generally is not, so a re-solve may legitimately return a
  different schedule at an identical cost. The derived zone is therefore **replace-on-rerun** rather
  than append-with-hash, the claim is **value-stable, not hash-guaranteed**, and every test and mart
  asserts on cost and on invariants — never on a specific hour's charge. `solver`, `solver_version`
  and an `input_digest` (of the *inputs*, not the results) are recorded so a divergence can be
  attributed rather than argued about. One invariant does not cover a whole platform.
- **Coverage windows are read from `dbt_project.yml`, not copied.** The optimizer parses the same
  `coverage_windows` var the hourly spine is generated from — the same "one file, two readers"
  pattern the tariff catalogue uses — so the two cannot drift. It also means the windows are
  DST-correct on both sides: 167 hours and 169 for the two DST weeks — never 7 × 24 — and a mart
  short of that count fails rather than being optimized as if it were whole. Windows must not
  overlap, since the spine expands `generate_series` per window with no `UNION`; the spring window
  abuts the March one exactly, and a test asserts the whole declared set stays disjoint.

## ML forecasting (M7)

[`src/energy_platform/forecasting/`](src/energy_platform/forecasting/) produces day-ahead hourly PV
and household-load forecasts and scores them in `mart_forecast_eval`, beside the naive baselines
they have to beat. Everything below runs on the synthetic demo data, and the section is careful
about which of it is a claim about forecasting and which is a claim about the simulator.

| `model_key` | role | what it is |
| --- | --- | --- |
| `persistence` | baseline | same local hour of the most recent **observable** day |
| `seasonal_naive` | baseline | same local hour of the most recent observable **same weekday** |
| `toy_physical` | **oracle** | M3's flat-plate model — *the process that generated the labels* |
| `pvlib_physical` | model | full plane-of-array chain: solar position → Hay-Davies → cell temperature → PVWatts |
| `pvlib_hgb` | model | the above plus a gradient-boosted residual correction, p10/p50/p90 |
| `load_hgb` | model | gradient boosting on calendar, solar geometry and forecast temperature |

```bash
just dbt-seed      # prices, weather, telemetry + synthetic forecast vintages
just warehouse     # dbt -> dispatch -> forecast -> forward-dispatch -> the read-back marts
just forecast --target pv_production_kwh   # re-run one target
```

### What the seeded numbers say, and what they don't

Mean absolute error over the spring window, 216 scored hours per model:

| target | model | role | MAE (kWh) |
| --- | --- | --- | --- |
| load | `load_hgb` | model | **0.0302** |
| load | `seasonal_naive` | baseline | 0.0314 |
| load | `persistence` | baseline | 0.0338 |
| pv | `toy_physical` | *oracle* | 0.1667 |
| pv | `pvlib_hgb` | model | **0.1801** |
| pv | `pvlib_physical` | model | 0.3108 |
| pv | `seasonal_naive` | baseline | 0.4506 |
| pv | `persistence` | baseline | 0.4788 |

**The load result is a ceiling, not a win.** M3 generates load as a fixed weekday × hour × month
profile times `1 + U(−0.12, 0.12)`. That noise is i.i.d. by construction and therefore not
learnable, so a perfect predictor of the shape still scores `E|U(−a,a)| = a/2` times the mean level
— **0.0274 kWh** here, in closed form. `load_hgb` reaches 0.0302 against that floor. Matching
seasonal-naive is the *correct* outcome: seasonal-naive on this data essentially is the generator.
A model that beat it substantially would be reading something it should not.

**The PV ranking is dominated by geometry, not by skill.** The synthetic truth is
`dc_kwp × GHI/1000 × PR × temp_derate` — irradiance on a *horizontal* surface, no panel tilt
anywhere. So `toy_physical` is not a competitor, it is the oracle, and `pvlib_physical` transposing
onto a 35° south-facing plane is biased against a truth that has no plane: on a clear April day the
tilted model collects **10% more** than the flat one. That is correct physics losing to a toy for
reasons that have nothing to do with forecasting.

`pvlib_hgb` fits the **residual** against that chain, and the number to read is the gap it closes,
not its rank: 0.3108 → 0.1801 is the boosting learning most of the transposition back off again. It
does not reach the oracle and should not — the oracle *is* the generator, and the only way past it
is to read the labels. A hybrid that beat 0.1667 on this data would be evidence of a leak, which is
why the ranking is reported with the oracle in it rather than filtered down to a flattering top row.

Design decisions worth calling out:

- **The oracle is a column, not a footnote.** `mart_forecast_eval.role` marks `toy_physical` as
  `oracle` on synthetic windows, so ordering the mart by `mae_kwh` without filtering is *visibly*
  wrong rather than quietly wrong, and `tests/dbt/test_forecast_reconciliation.py` asserts the
  label. A caveat that lives only in a README is a caveat that gets skipped by whoever builds the
  chart.
- **A window too short to judge a model on reports baselines and no model.** The two DST weeks are
  seven days with no history behind them, so `seasonal_naive` can never resolve an equivalent hour
  there and nothing can be fitted. They appear in `mart_forecast_eval` with their baselines and
  nothing else — not omitted, which would let `assert_baselines_exist_for_every_model` pass over a
  window nobody could see was missing, and not padded with physical models, which would put a model
  row in the mart with no baseline beside it.
- **M7 ships a falsifiable prediction.** On real Fenecon telemetry the PV ranking should *invert* —
  a real array is tilted, so plane-of-array physics recovers accuracy exactly where the flat-plate
  toy loses it. That is checkable once the private telemetry accumulates, and it is a better claim
  than any number this table could contain.
- **A synthetic-trained model is refused for real prediction.** The residual model has learned to
  undo a transposition real data needs; outside the simulator that is a confident error, not
  knowledge. Every artifact records its `training_data_source` and `load_artifact` raises rather
  than warning. The known limitation is an interlock, not a note. It guards *reuse*, and M7 reuses
  nothing: the backtest refits per fold, so it writes no artifact and `forecast_runs.artifact_key`
  is null on every M7 row — pinned by a test, so the column cannot start claiming otherwise while
  this paragraph still says it does not. The interlock is the seam M8's serving path loads through.
- **"Persistence = yesterday" is lookahead here, and the harness says so.** Synthetic telemetry is
  generated from ERA5-backed archive weather that settles ~5 days late, so yesterday's PV has not
  been observed when today's forecast is issued. Baselines walk back to the most recent
  *observable* equivalent hour — six or seven days, not one. A harness that allowed the textbook
  version would report a baseline nothing could honestly beat.
- **The vintage rule is stated once and enforced everywhere.** *For target Berlin day D, use the
  last stored vintage whose `issue_time` is strictly before D 00:00 Europe/Berlin.* One function,
  reusing the same `berlin_day_window` the hourly spine and the coverage windows use, so DST is
  handled in one place. The rule id is persisted on every prediction row.
- **The lookahead test is the milestone.** `tests/forecasting/test_lookahead_rejection.py` makes
  four deliberate attempts to cheat — reading the target hour's own actual, a vintage issued after
  the prediction, a lag shorter than the observation lag, and a feature that declines to declare
  when it became knowable — and asserts each is refused. It ships with a **positive control**,
  because a checker that rejected everything would pass all four.
- **DNI is recovered exactly, not decomposed.** Open-Meteo's `direct_radiation` is direct
  *horizontal* irradiance, so `DNI = direct_radiation / cos(zenith)`. The usual physical hybrid has
  to estimate DNI with Erbs or DISC and inherit that model's error; this one does not, because M2
  ingested all three components. It is also why the synthetic vintage generator scales all three by
  one shared factor — perturbing them independently would break the identity silently.
- **Synthetic forecast vintages exist because real ones cannot be backfilled.** Open-Meteo serves
  only the current issue, so the demo had a forecast in 2026 and telemetry in 2024 and no overlap
  at all. The generator degrades archive actuals with a per-vintage regime offset and lead-time-
  growing noise — correlated, because white noise would be averaged away and make a residual
  correction look far better than a real one ever does. It is stdlib-only and content-hashed like
  every other producer, and it is deliberately **never scheduled**: it reconstructs an issue-time
  snapshot from data that settles days *after* the instant it claims.
- **Intervals are reported, never claimed.** p10/p90 coverage lands at 0.84 for PV and 0.71 for
  load against a target of 0.80. The p10 fit is driven by a few hundred tail rows, so the honest
  move is to print the number rather than assert calibration.
- **The dependency cost is contained, and the containment is tested.** pvlib and scikit-learn bring
  numpy, pandas and scipy — the cost M6 declined to pay for linopy. `tests/test_import_containment.py`
  walks the AST of every first-party module and fails if any of them is imported outside
  `forecasting/`, plus a subprocess check that importing the CLI does not load them and a positive
  control that `forecasting/` does. The claim is precise: no *first-party* module outside
  `forecasting/` touches the scientific stack. Not that the process is numpy-free — highspy has
  pulled numpy in since M6, and stretching the claim to cover that would make it false.
- **Model training is a third, weaker determinism tier — and `random_state` is not the lever.**
  It governs binning subsampling (inert below 200 000 samples) and the early-stopping split, which
  is off below 10 000 samples. What actually moves the last bits of every split threshold is
  OpenMP: histogram accumulation is threaded, so summation order follows `OMP_NUM_THREADS`. It is
  pinned to 1 in `just forecast` and in CI, `early_stopping` is set explicitly rather than left to
  a default that changes with the data size, and the claim is: same thread count and library
  versions → identical predictions; different thread count → identical metrics to reported
  precision, not identical bits. The `config_hash` covers the fit's *inputs*, never the fitted
  bytes.

## Forward-looking dispatch (M8)

M6 asked what a perfect battery schedule would have cost. M7 asked how well tomorrow can be
predicted. [`src/energy_platform/dispatch/forward.py`](src/energy_platform/dispatch/forward.py) joins
them: for every Berlin day it plans a schedule from the forecasts available at that day's decision
time, **executes that plan against the day that actually happened**, and carries the battery's state
into tomorrow along the executed trajectory rather than the planned one. `mart_dispatch_regret` then
answers the question the whole platform was built for — of the savings perfect information would
have produced, how much did real forecasts capture?

| Scenario | What it is |
| --- | --- |
| `naive_continuous` | M3's self-consumption policy, SoC carried. What the battery gives you for free |
| `forecast_driven` | day-ahead plans from M7 forecasts, executed against actuals. The deliverable |
| `perfect_foresight_plan` | the *same* rolling planner, handed the actuals. A decomposition instrument |
| `optimal` | the hindsight MILP over the same actuals. The ceiling |

```bash
just warehouse          # dbt -> dispatch -> forecast -> forward-dispatch -> the four read-back marts
just forward-dispatch   # re-simulate on an already-built warehouse
just figure             # regenerate the figure above from the marts
```

### The headline is negative, and that is the result

Sixty rolling days, 2024-05-02 to 06-30 — the part of the spring window where a model has warmed up.

| | dynamic tariff | static tariff |
| --- | --- | --- |
| Available savings (naive → hindsight) | **€0.29** | **€0.00** |
| Forecast-driven, vs naive | **−€2.84** (worse) | **−€2.86** (worse) |
| — of which forecast error | €2.86 | €2.86 |
| — of which day-ahead myopia | €0.27 | €0.00 |
| **Captured value share** | **−965%** | n/a (no prize) |
| Attainable share, perfect forecast | **6.7%** | n/a |

**Captured-value share is the number to quote, and here it is deeply negative.** It normalises by the
size of the prize, so it is comparable across windows of different length and sunniness in a way a
euro figure is not — and it says plainly that on this system, dispatching on forecasts is worse than
not dispatching on anything. The euro figures explain why: the prize is 29 cents over two months, and
being wrong about the weather costs ten times that.

**None of these euros annualise, and the share is the reason they do not have to.** Every figure in
the table is a total over sixty specific spring days of synthetic data, and multiplying it by six
would carry all of [the objections raised against annualising M6](#the-headline-honestly) plus a new
one: forecast error is seasonal, so a spring window says little about December. That is precisely
why `mart_dispatch_regret`'s documentation instructs readers to quote `captured_value_share` and not
a euro total, and why the dashboard shows the euro prize only next to the span it was measured over.

Design decisions worth calling out:

- **The result is reported, never asserted — and that is a deliberate hole in the test suite.**
  `optimal ≤ forecast_driven` and `optimal ≤ naive` are theorems and both are asserted, in the
  property tests and in `assert_hindsight_never_costs_more_than_either.sql`. `forecast_driven ≤ naive`
  is **not** a theorem and is asserted nowhere. Naive self-consumption is *reactive* — it looks at the
  meter and needs no forecast at all — while a day-ahead plan commits in advance and is wrong whenever
  the forecast is. Asserting the ordering would have made the suite fail exactly when the platform
  produced its most honest output, and would have created quiet pressure to improve the seeded data
  until it passed. `test_a_bad_forecast_may_underperform_naive_and_is_reported_not_asserted` constructs
  that case on purpose and asserts the *reporting* of it, so the missing assertion is a decision on the
  record rather than an oversight.
- **Regret decomposes, and reporting it undivided would have blamed the wrong thing.** A day-ahead
  controller cannot move energy across a midnight it has already passed; the hindsight optimum can. So
  `perfect_foresight_plan` runs the identical loop with the actuals substituted for the forecast, and
  the gap it still leaves is `myopia_cost_eur` — a property of the decision horizon that no model
  improvement could recover. On the dynamic tariff that is €0.27 of a €0.29 prize: **essentially all
  of the optimizer's value is multi-day arbitrage, and a day-ahead controller structurally cannot
  reach it.** The first version of this milestone reported €3.51 of regret as though it were all
  forecast error. It was not.
- **The planner's continuation value is not the optimizer's terminal value, and using one for the
  other cost €19.** M6 prices energy left in the battery at the cheapest non-negative import price the
  window offered, which is right for a boundary the optimizer meets once. M8's planner meets a
  boundary *every night*, and tomorrow starts from exactly the state tonight leaves — so it needs a
  **continuation value**, and that quantity is bounded from below in closed form: storing energy earns
  `λ·rte` where exporting it earns the feed-in rate, so below `feed_in / rte` the planner sells the
  battery off every evening. With this catalogue that floor is **9.01 ct/kWh**, and the seeded sweep
  puts the cliff exactly there — €19.5 of avoidable loss at 8.11 ct, none at 10 ct. Between that floor
  and the **median** import price the answer is flat (€0.00 static / €0.27 dynamic across 10–34 ct/kWh),
  so the midpoint of the two is taken: derived from the tariff and the battery, no free constant. The
  upper bound is the median and deliberately *not* M6's minimum, because the two functions bound
  different things — M6 credits energy at the end of a window it will never see again, while a rolling
  planner's energy is going to be used *tomorrow*, and the cheapest hour of a sixty-day window is not an
  estimate of that. Clamping to the minimum was tried and measured: under the dynamic tariff the
  cheapest retail hour is 6.72 ct/kWh, *below* the 9.01 ct floor, so the band inverts, the planner lands
  on the cliff, and €3.13 of regret becomes €19.97. `ENERGY_DISPATCH_CONTINUATION_CT_KWH` reproduces
  both sweeps.
- **Day-ahead prices have no publication timestamp anywhere in the warehouse, so M8 adds the rule.**
  Prices arrive through the settled-data path; the only temporal provenance a price row carries is
  `raw.ingestion.fetched_at`, which records when *this platform* fetched the number rather than when
  the exchange published it. Inventing a per-row instant would be fabricating provenance the source
  never supplied, so the market's timetable is written down instead — one function, one rule id
  (`day_ahead_auction_d_minus_1_1245_berlin`), persisted on every run — and `assert_prices_published`
  turns it into a refusal. The guard that matters is the horizon one: planning day D at D 00:00 is
  fine, extending the same plan to D+1 reads an auction that clears half a day *after* the decision,
  and that is exactly how the bug would be written.
- **The decision time was chosen by M7, not by M8.** `vintage.py` already declared D 00:00
  Europe/Berlin as "the information set M8 needs", and the synthetic vintage generator stamps its
  issues at 18:00 the evening before. Moving the decision to, say, D−1 14:00 — which sounds more
  realistic — would have made every stored vintage on the platform inadmissible and bought nothing.
  A test asserts the two modules resolve the same instant on four days including both DST Sundays.
- **The recourse policy is a feasibility projection, and what is actually infeasible is narrower than
  it looks.** The instinct is that "planned to charge, but the sun did not come out" is the failure
  case. It is not — the MILP permits grid charging, so the plan is honoured by importing, and the
  money is worse. That is how a forecast error *should* show up. Only the store's own limits are
  genuinely unreachable, so execution clips both legs to the SoC band and the power ratings and lets
  the grid close the balance. That keeps the executed trajectory inside the optimizer's feasible set,
  which is what makes `optimal ≤ forecast_driven` a theorem rather than a hope. Hourly re-planning
  (MPC) is the stronger controller and is out of scope: it needs an intraday forecast update this
  platform does not ingest, and it would measure the re-planner rather than the day-ahead forecast.
- **`clipped_hours` is zero, and getting there found a real defect.** The flag started as an exact
  float comparison, so a plan that executed *exactly as written* still reported ~25 clipped hours out
  of 1440 — the SoC headroom the executor recomputes differs from the solver's in the last bits. A
  metric reporting recourse where none happened, on the one column a reader would use to judge whether
  the plans were feasible at all. It now compares at 0.1 Wh, the same quantum the optimizer calls zero.
- **The simulated span is not the declared window, and every number is over the span.** M7 fits
  nothing until 28 days of history exist and then refits weekly, so the simulation covers 60 of the
  spring window's 88 days and neither DST week at all. All four scenarios are re-solved over exactly
  the simulated hours — comparing a forecast-driven result over 60 days against a naive baseline over
  88 would be the most flattering possible arrangement and the least meaningful. The two DST weeks
  appear in the mart with `is_simulated = false` and `not_simulated_reason = 'no_fitted_model'`, not
  absent: a window that vanishes lets every coverage test downstream pass over something nobody can
  see is missing, which is precisely how those two weeks went unnoticed in `mart_forecast_eval` at M7.
- **A day with no usable forecast falls back to self-consumption rather than breaking the chain.**
  Two of the sixty days have no complete forecast. Dropping them would put a hole in the SoC chain;
  idling the battery would be a worse controller than the fallback and would misattribute the loss.
  So M3's own `dispatch_hour` runs those days — legitimately, because naive self-consumption is
  reactive and needs no knowledge of the future — and `fallback_days` reports how much of the
  comparison rested on it.
- **M8 is the first thing to walk M7's provenance interlock.** `save_artifact`/`load_artifact` existed
  since M7 with *zero* production call sites: the backtest refits per fold and discards, so
  `artifact_key` was null on every row and the interlock guarded a path nobody used. The serving path
  fits one model per fold, persists it, and **reads it back through `load_artifact`** before serving —
  so a synthetic-trained model is refused for real prediction on the production path rather than in a
  test. The planner also refuses a model whose role is not `model`: on synthetic windows
  `toy_physical` *is* the generator, and planning with it would land forecast-driven dispatch on top
  of the hindsight optimum and report a triumph that measured the simulator.
- **The figure's two panels measure two different quantities, and it says so.** The bars are adjusted
  net cost — the objective, the only rankable quantity. The cumulative curves are energy cost only,
  because the terminal credit belongs to the whole span and cannot honestly be attributed to a single
  day. Their endpoints differ by that credit, and the footnote explains it rather than leaving a
  reader to assume an error. `scripts/report_regret.py --check` recomputes the plotted numbers and
  diffs them against a committed JSON sidecar, so CI fails a stale figure on its *values* — never on
  PNG bytes, which differ across font stacks and would make the check about rendering.
- **M8 ships a falsifiable prediction, like M7 did.** On real Fenecon telemetry the captured-value
  share should *rise* — not because the forecasts get better (they will get worse; the synthetic load
  target is a fixed profile plus i.i.d. noise, so `load_hgb` is already near the irreducible floor)
  but because the **prize** grows. Real load is spikier and less correlated with real PV than the
  generator's, so there is more genuine arbitrage for the optimizer to find, and a bigger denominator
  is what turns −965% into a number worth acting on. If the share stays negative once real telemetry
  accumulates, the honest conclusion is that this battery should be run on self-consumption and the
  optimizer left off — which is a result, and one this repo would print.

## Dashboard (M9)

A Streamlit app in `dashboard/`, served by `docker compose` on
[localhost:8501](http://localhost:8501) and runnable on the host with `just dashboard`. Four pages,
each reading only the marts it names:

| Page | Reads | Shows |
| --- | --- | --- |
| **Overview** | `mart_hourly_energy`, `mart_coverage_monthly`, `mart_data_quality` | Hourly PV, load, import, export and state of charge over a covered span; coverage per month with partial months flagged; per-source gaps and freshness |
| **Economics** | `mart_tariff_counterfactuals`, `mart_solar_economics` | {static, dynamic} × {battery, no battery} net cost per month; self-consumption and autarky; avoided grid cost against feed-in revenue |
| **Dispatch** | `mart_dispatch_regret`, `mart_forward_dispatch_daily`, `mart_dispatch_comparison` | Captured-value share beside the attainable share, the regret split, the cumulative cost curve, and M6's four-scenario table ranked by `adjusted_net_cost_eur` |
| **Forecasts** | `mart_forecast_eval` | MAE, bias, RMSE and pinball per model per horizon, split by `role` so the oracle is labelled rather than ranked |

A persistent banner on every page states the active data mode — *demo: synthetic data* or *real* —
read from `mart_data_quality.data_mode`, not from a config flag or a hardcoded string.

Design decisions worth calling out:

- **The mart-only rule: presentation computes nothing.** Every number on screen is a column of a
  dbt mart. The app pivots, sorts and formats; it does not re-derive a cost, a rate, a coverage
  figure or a metric. If a figure is not in a mart, the fix is a mart — and that is not a slogan
  here, it is what happened twice. `mart_coverage_monthly` exists because `mart_data_quality` has
  no monthly grain and the Overview page needed one. `mart_dispatch_regret.attainable_savings_eur`
  exists because the Dispatch page wanted `naive − perfect_foresight`, which the mart had been
  computing *inside* a ratio and never exposing. Both were one-line temptations to add a `groupby`
  or a subtraction in Python, and both would have put a number on screen that no test in this repo
  could check.
- **The rule is enforced three ways, because no one of them is sufficient.** *The database
  refuses*: the app connects as `dashboard_ro`, which holds `SELECT` on `analytics_marts` and
  nothing else, with `default_transaction_read_only` on — `raw`, `derived`, staging and
  intermediate are not merely unread, they are unreadable. *A static guard*: `test_mart_only.py`
  walks the AST of every module under `dashboard/` and fails if any module but the query layer
  imports a driver or holds a SQL string. *A contract guard*: every query is declared as data — a
  mart name and its columns — so CI can check each column against the built warehouse, which is
  what turns "change the mart" into a build failure. The static guards cannot see a query composed
  at runtime; the role can. The role cannot tell a mart column from a Python expression; the
  guards can. Each carries a positive control, so a guard cannot pass by the thing it guards
  having been deleted.
- **This completes an argument the repo has been making since M4.** Ingestion is bit-reproducible
  and content-hashed. Transformations live in tested SQL. Optimization is value-stable. And now
  presentation computes nothing. Every layer's claim is checkable in exactly one place, which is
  the property that makes any of them worth stating.
- **The empty warehouse is the happy path, not an edge case.** A stranger clones the repo, runs
  `just demo`, and opens the dashboard before anything is seeded — that is the first thing that
  happens to this app, every time. So every page renders a designed "no data yet" state naming the
  two commands that fix it, and `tests/dashboard/test_empty_warehouse.py` asserts all four reach it
  with no traceback. It runs in the fast CI job on every push, where the seeded tests can only
  skip. The state is reached by *probing* `information_schema` rather than by catching
  `UndefinedTable`: swallowing that exception would render a mart name the app got **wrong** as a
  friendly onboarding message, hiding a bug behind reassurance.
- **Coverage travels with every euro figure.** The demo covers three short windows, so a monthly
  total read as a monthly bill is wrong by a large factor. Each page puts `completeness_ratio` and
  `is_partial_month` / `is_partial_window` next to the figures they qualify, and the battery-versus-
  no-battery views carry the `battery_unreturned_kwh` caveat about M3's midnight state-of-charge
  reset.
- **Shares are never clamped.** `captured_value_share` is bounded above by 1 (a theorem) and
  deliberately unbounded below. On the seeded data it is −965%, and the Dispatch page shows exactly
  that, beside the attainable share that explains it. A UI that clipped the axis to the good half
  would suppress the most interesting result in the project.
- **Streamlit is an optional extra, not a runtime dependency.** `uv sync --extra dashboard`; the
  Dockerfile installs it in one stage so the Dagster webserver and daemon images never carry it.
  The boundary is enforced rather than intended — `tests/test_import_containment.py` fails if
  anything under `src/energy_platform` imports `streamlit` or `altair`, which is the same fence
  matplotlib has had since M8, and it also keeps the dependency pointing one way: `dashboard/`
  imports the package, never the reverse.
- **Altair rather than a new plotting stack.** It already ships inside Streamlit, so it adds no
  wheel, and it gives the encoding control three charts genuinely need: colour bound to a scenario
  key rather than to its position, a role set apart without implying a rank, and a diverging axis
  around zero. Colours come from `energy_platform.palette`, the module `scripts/report_regret.py`
  draws the README figure from — so blue means *forecast-driven* in the figure and on the page by
  construction rather than by coincidence.

## Engineering invariants

- **Idempotent, re-runnable ingestion** — content-hash verification, safe backfills.
- **Append-only raw zone** — transformations are views/models, never mutations.
- **No lookahead** — asof joins, time-series CV, forecasts use only data available at
  prediction time, and *decisions* use only prices whose auction had cleared at the decision time.
- **NaN over fabrication** — missing intervals stay missing and are surfaced by dbt tests.
- **Reproducible builds** — `uv.lock` committed; CI and the Docker image install `--frozen`.
- **Claims are scoped to what holds** — three tiers, deliberately not one. Ingestion is
  bit-reproducible and content-hashed. Optimization results are value-stable but explicitly *not*
  hash-guaranteed, because an optimal schedule is not unique. Model training is weaker still:
  bit-reproducible only at a pinned thread count, with no cross-platform claim at all. Stretching
  any one of these to cover the others would make it false.
- **Presentation computes nothing** — every number the dashboard shows is a column of a mart. The
  app reshapes and formats; it never re-derives a cost, a rate or a metric. Enforced by a read-only
  role that can see the marts schema and nothing else, an AST guard confining SQL to one module, and
  a per-column contract checked against the built warehouse in CI. If a figure is missing, the fix
  is a mart.
- **Orderings are asserted only where they are theorems** — the hindsight optimum is provably never
  worse than any trajectory feasible for its own problem, and that is enforced in the property tests
  and in the warehouse. "Forecasting beats doing nothing clever" is an empirical result about a
  dataset, so it is reported and never asserted; on the seeded data it is currently false.

## Privacy

All code, models, and the optimizer are public. Real household telemetry never enters the
repo — a synthetic sample dataset ships so `just demo` shows the full flow for anyone.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), [just](https://github.com/casey/just), and Docker.

```bash
just install     # install locked dependencies
just check       # lint + strict type-check + tests
just demo        # boot the stack -> Dagster :3000, dashboard :8501
just dbt-seed    # offline demo data into the raw zone
just warehouse   # build every mart
just demo-down   # stop and drop the Postgres volume
```

`just demo` boots the stack detached and prints both URLs. It does **not** load data: the dashboard
opens in a designed "no data yet" state naming the next two commands, which is where a stranger's
first click actually lands. Timings measured on the author's machine, and stated separately because
they differ by two orders of magnitude:

| | |
| --- | --- |
| `just demo`, images already built | **~8 s** to a serving dashboard |
| `just demo` on a fresh clone | **a few minutes** — one `docker build`, almost all of it installing locked dependencies, paid once |
| `just dbt-seed` + `just warehouse` | **minutes** — offline ingestion, then a dbt build, sixteen model fits and sixty days of MILP |

The last row is why seeding is a separate step and not folded into `just demo`. It could be, and
then the demo would take minutes and the sixty-second promise would quietly be false; a stack that
boots in seconds into an honest empty state is the better trade. Both seeding commands are offline —
recorded fixtures, no API keys.

## Development

| Command          | Purpose                                        |
| ---------------- | ---------------------------------------------- |
| `just lint`      | ruff lint + format check                       |
| `just fmt`       | auto-fix and format                            |
| `just typecheck` | mypy (strict)                                  |
| `just test`      | pytest                                         |
| `just backfill`  | load market + weather history into the raw zone |
| `just dbt-seed`  | offline-seed the raw zone (recorded fixtures)  |
| `just dbt-build` | build dbt models + run all tests               |
| `just forecast`  | backtest the forecast models (`OMP_NUM_THREADS=1`) |
| `just forward-dispatch` | roll the M8 day-ahead simulation over every window |
| `just figure`    | regenerate the README's three-way comparison figure |
| `just forecast-reset` | drop synthetic vintages so a changed generator can re-seed |
| `just dbt-reconcile` | assert the Python tariff engine matches the SQL |
| `just dbt-docs`  | generate the dbt docs site                     |
| `just dashboard` | run the Streamlit dashboard on the host        |
| `just dashboard-grants` | create/refresh the dashboard's read-only role |
| `just demo-logs` | follow the detached stack's logs               |
| `just lock`      | regenerate `uv.lock` after changing deps       |

Any backfill accepts `--offline` (or `ENERGY_OFFLINE=1`) to serve SMARD/Open-Meteo from recorded
fixtures instead of the live APIs — deterministic and network-free, which is how CI and the demo
seed the stack.

CI runs the full quality gate (against a Postgres service container, so the raw-zone idempotency
tests execute), a `dbt` job that rebuilds and tests the warehouse on offline-seeded data, solves the
dispatch optimizer, backtests the forecast models, rolls the forward-looking simulation, builds the
four read-back marts, asserts the committed README figure still matches the warehouse, and then runs
the warehouse guards — the no-lookahead manifest check and the Python↔SQL tariff, dispatch and
forecast-metric reconciliations, all of which hard-fail rather than skip there — plus the dashboard
smoke tests, which render all four pages against the built warehouse and assert every column the app
selects exists — and `docker compose config` validation on every push and PR; the dbt docs site
publishes to GitHub Pages on merge to main.

The dashboard's empty-warehouse tests run in the *quality* job instead, on every push: they need no
marts, which is the point of them, and a graceful-degradation test that only ever runs against data
proves nothing.

## License

[MIT](LICENSE)
