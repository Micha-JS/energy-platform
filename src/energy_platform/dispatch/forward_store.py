"""Persisting the forward-dispatch simulation, and the one thing its contract adds to M6's.

Same shape and the same replace-on-rerun contract as :mod:`energy_platform.dispatch.store`: this
module owns every piece of SQL, hands frozen dataclasses to a pure core that never sees a
connection, and makes no claim that a re-run reproduces a byte-identical schedule. The reasons are
M6's, plus one more -- a plan here comes out of a *fitted model*, and a fit is only reproducible on
a machine with the same thread count and library versions. Two irreproducible components in series
is not a reason to weaken the claim; it is a reason to record enough provenance to attribute a
divergence, which is what ``config_hash``, the model keys and the rule ids are for.

**Three tables, because the simulation answers three different questions.**

``forward_dispatch_runs`` is the headline grain -- one row per (region, window, tariff, scenario)
over the *simulated span*, which is generally shorter than the declared window and is recorded as
its own pair of dates rather than being inferred. ``forward_dispatch_days`` is where the rolling
structure is visible: the SoC chain day by day, whether each day was planned or fell back, and how
many hours the plan had to be clipped in. ``forward_dispatch_schedule`` carries the hourly executed
flows *and the planned ones beside them*, so plan-versus-reality is inspectable rather than being
something the reader has to take on faith.

**A window that could not be simulated still gets a row.** ``forward_dispatch_runs`` records it with
null costs and a ``not_simulated_reason``. Omitting it would let the mart's coverage test pass over
a window nobody could see was missing -- the same trap M7 fell into with the two DST weeks, and the
same resolution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.decision import DECISION_RULE_ID, PRICE_PUBLICATION_RULE_ID
from energy_platform.dispatch.execution import ExecutedHour
from energy_platform.dispatch.forward import (
    ForwardSolution,
    NotSimulated,
    PlannedHourContext,
    local_date_of,
)
from energy_platform.dispatch.model import DispatchHour, DispatchResult, Scenario, round_kwh
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.forecasting.vintage import SELECTION_RULE_ID

# The tariff a not-simulated marker is filed under. A window nothing could be simulated for has no
# per-tariff result to report -- the reason is about the models, not the money, so it is the same
# for every tariff -- and one row says it once rather than N rows saying it N times.
#
# It is a value in the same column as the real tariff ids, so the two write paths overlap in the
# key space and each has to clear the other's rows -- see `replace_window`. They are not two
# independent partitions of the table; they are two mutually exclusive claims about one window.
ALL_TARIFFS: Final = "*"

# What `plan_status` says for a scenario whose per-day decision this table does not carry. Three
# scenarios take it, for two different reasons: naive_continuous and optimal were never planned at
# all, being solved once over the whole span, while perfect_foresight_plan *is* rolled day by day
# but is a decomposition instrument rather than a deliverable, so `forward.simulate` keeps only its
# settlement and discards its day outcomes. All three are still costed per day, because the README
# figure needs all four cumulative curves to plot three differences against naive. Writing the
# scenario name into this column instead would make it a second, redundant copy of `scenario`
# rather than an honest "does not apply".
PLAN_STATUS_NOT_PLANNED: Final = "not_planned"


@dataclass(frozen=True, slots=True)
class ForwardWriteCounts:
    """What one replace-write did, so the CLI can report it and a test can assert on it."""

    runs: int
    days: int
    hours: int
    replaced: int


def _ddl(schema: str) -> list[sql.Composed]:
    ident = sql.Identifier(schema)
    return [
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.forward_dispatch_runs (
                id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                region                  text        NOT NULL,
                window_start            date        NOT NULL,
                window_end              date        NOT NULL,
                tariff_id               text        NOT NULL,
                scenario                text        NOT NULL,
                -- The span actually simulated. Null on a window that could not be simulated at
                -- all, which is why these are not the window columns above under other names.
                sim_start               date,
                sim_end                 date,
                simulated_days          integer     NOT NULL DEFAULT 0,
                fallback_days           integer     NOT NULL DEFAULT 0,
                is_simulated            boolean     NOT NULL,
                not_simulated_reason    text,
                solver                  text,
                solver_version          text,
                status                  text,
                terminal_value_ct_kwh   double precision,
                terminal_soc_delta_kwh  double precision,
                terminal_discharge_efficiency double precision,
                soc_start_kwh           double precision,
                soc_end_kwh             double precision,
                energy_cost_eur         double precision,
                feed_in_revenue_eur     double precision,
                net_cost_eur            double precision,
                terminal_value_eur      double precision,
                objective_eur           double precision,
                battery_charge_kwh      double precision,
                battery_discharge_kwh   double precision,
                clipped_hours           integer     NOT NULL DEFAULT 0,
                priced_hours            integer     NOT NULL DEFAULT 0,
                expected_hours          integer     NOT NULL DEFAULT 0,
                -- Provenance. The rule ids mirror what M7 persists on every prediction row, so a
                -- stored result names the rules it was produced under rather than relying on a
                -- docstring having said the same thing at the time.
                pv_model_key            text,
                load_model_key          text,
                decision_rule_id        text        NOT NULL,
                price_publication_rule_id text      NOT NULL,
                selection_rule_id       text        NOT NULL,
                training_data_source    text        NOT NULL,
                battery                 jsonb       NOT NULL,
                input_digest            text        NOT NULL,
                simulated_at            timestamptz NOT NULL DEFAULT now()
            )
            """
        ).format(schema=ident),
        # One row per scenario per (site, window, tariff). UNIQUE rather than a plain index: the
        # replace-write relies on it, and a duplicate would double every mart total.
        sql.SQL(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS forward_dispatch_runs_key
            ON {schema}.forward_dispatch_runs
               (region, window_start, window_end, tariff_id, scenario)
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.forward_dispatch_days (
                run_id                bigint NOT NULL
                    REFERENCES {schema}.forward_dispatch_runs (id) ON DELETE CASCADE,
                region                text        NOT NULL,
                window_start          date        NOT NULL,
                window_end            date        NOT NULL,
                tariff_id             text        NOT NULL,
                scenario              text        NOT NULL,
                local_date            date        NOT NULL,
                decision_time         timestamptz NOT NULL,
                plan_status           text        NOT NULL,
                -- Nullable, and the nulls are meaningful. Only forecast_driven has a state of
                -- charge chained day to day from its own executed trajectory; the two reference
                -- scenarios are solved once over the whole span and merely settled per day, so
                -- they have no per-day boundary state to report. A zero here would be a fabricated
                -- reading of an empty battery. The not-null tests in _marts.yml are scoped to the
                -- scenario the columns apply to.
                soc_start_kwh         double precision,
                soc_end_kwh           double precision,
                clipped_hours         integer     NOT NULL,
                clamped_forecast_hours integer    NOT NULL,
                pv_fit_day            date,
                load_fit_day          date,
                energy_cost_eur       double precision,
                feed_in_revenue_eur   double precision,
                net_cost_eur          double precision,
                priced_hours          integer     NOT NULL
            )
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE INDEX IF NOT EXISTS forward_dispatch_days_run_idx
            ON {schema}.forward_dispatch_days (run_id, local_date)
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.forward_dispatch_schedule (
                run_id                bigint NOT NULL
                    REFERENCES {schema}.forward_dispatch_runs (id) ON DELETE CASCADE,
                region                text        NOT NULL,
                window_start          date        NOT NULL,
                window_end            date        NOT NULL,
                tariff_id             text        NOT NULL,
                scenario              text        NOT NULL,
                ts_utc                timestamptz NOT NULL,
                pv_production_kwh     double precision,
                household_load_kwh    double precision,
                battery_charge_kwh    double precision,
                battery_discharge_kwh double precision,
                soc_kwh               double precision,
                grid_import_kwh       double precision,
                grid_export_kwh       double precision,
                price_eur_mwh         double precision,
                import_price_ct_kwh   double precision,
                energy_cost_eur       double precision,
                feed_in_revenue_eur   double precision,
                is_priced             boolean     NOT NULL,
                -- Null for the three reference scenarios: only forecast_driven's plan is
                -- recorded, so only it has a plan to have departed from.
                planned_battery_charge_kwh    double precision,
                planned_battery_discharge_kwh double precision,
                was_clipped           boolean,
                -- What the plan EXPECTED, beside what it instructed (M10). The solve derives all
                -- of this and the first version of this table kept only the battery pair, so a
                -- consumer wanting to know what the house was expected to draw had to recompute it
                -- from the planned flows and the forecasts -- a second derivation of one plan, free
                -- to disagree with this one. The publisher reads these columns instead.
                -- Also null wherever `planned_battery_*` is: an hour the solve could not resolve
                -- expected nothing, and a zero here would be a fabricated reading.
                planned_pv_production_kwh     double precision,
                planned_household_load_kwh    double precision,
                planned_grid_import_kwh       double precision,
                planned_grid_export_kwh       double precision,
                planned_soc_kwh               double precision
            )
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE INDEX IF NOT EXISTS forward_dispatch_schedule_run_idx
            ON {schema}.forward_dispatch_schedule (run_id)
            """
        ).format(schema=ident),
    ]


class ForwardDispatchRepository:
    """Writes the forward-dispatch simulation's results to the derived zone.

    Deliberately write-only: the simulation's *inputs* are the same ``mart_hourly_energy`` hours
    M6's :class:`~energy_platform.dispatch.store.DispatchRepository` already reads, plus the
    forecasting store's observations and vintages. Adding a third reader of the same mart would be a
    third place for the "filter on local_date, not ts_utc" rule to be got wrong.
    """

    def __init__(
        self,
        conn: psycopg.Connection[tuple[object, ...]],
        *,
        derived_schema: str,
    ) -> None:
        self._conn = conn
        self._derived = derived_schema

    def ensure_schema(self) -> None:
        """Create the forward-dispatch tables if they are not there. Idempotent."""
        with self._conn.cursor() as cur:
            for statement in _ddl(self._derived):
                cur.execute(statement)
        self._conn.commit()

    def replace_window(
        self,
        solution: ForwardSolution | NotSimulated,
        battery: BatteryConfig,
        solver_version: str,
        training_data_source: str,
    ) -> ForwardWriteCounts:
        """Persist one (site, window, tariff), replacing any previous simulation of it.

        Delete-then-insert in one transaction, keyed on everything but the scenario, so a re-run
        supersedes the whole comparison rather than leaving one scenario from an older simulation
        beside two from a newer one. A :class:`NotSimulated` window writes a single marker row and
        no schedule -- present and explained, rather than absent and unexplained.

        **The delete also takes out the marker**, and the marker path takes out every tariff's
        rows. A window can move between the two states in either direction -- ``just
        forecast-reset`` or a higher ``min_train_days`` makes a simulated window unsimulable, and
        the models warming up does the reverse -- and a delete scoped to its own ``tariff_id``
        would leave the losing side's rows in place. The mart would then report a previous run's
        costs as current beside a marker saying nothing was simulated, and every coverage
        assertion downstream would pass, because none of them can see that a row is stale.
        """
        if isinstance(solution, NotSimulated):
            return self._replace_not_simulated(solution, battery, training_data_source)

        digest = _input_digest(solution, battery)
        params = _battery_json(battery)

        with self._conn.transaction(), self._conn.cursor() as cur:
            replaced = self._delete(
                cur,
                solution.region,
                solution.window,
                (solution.tariff_id, ALL_TARIFFS),
            )
            days_written = hours_written = 0
            for result in solution.results:
                run_id = self._insert_run(
                    cur, solution, result, params, digest, solver_version, training_data_source
                )
                hours_written += self._insert_schedule(cur, solution, result, run_id)
                days_written += self._insert_days(cur, solution, result, run_id)

        return ForwardWriteCounts(
            runs=len(solution.results),
            days=days_written,
            hours=hours_written,
            replaced=replaced,
        )

    def _replace_not_simulated(
        self,
        marker: NotSimulated,
        battery: BatteryConfig,
        training_data_source: str,
    ) -> ForwardWriteCounts:
        """Record that a declared window produced nothing, and why.

        Written under :attr:`Scenario.FORECAST_DRIVEN` alone. The other three *could* be computed
        here -- M6 already reports two of them over the full window -- but they would be over a
        different span than any simulated row, and a mart that mixed the two grains under one set
        of column names would be the single most misleading thing this milestone could ship.

        The delete is scoped to the window and **not** to a tariff. "Nothing could be simulated" is
        a claim about the models, so it is a claim about every tariff at once, and any per-tariff
        rows a previous run left behind are now false rather than merely old.
        """
        with self._conn.transaction(), self._conn.cursor() as cur:
            replaced = self._delete(cur, marker.region, marker.window, None)
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {schema}.forward_dispatch_runs (
                        region, window_start, window_end, tariff_id, scenario,
                        is_simulated, not_simulated_reason,
                        decision_rule_id, price_publication_rule_id, selection_rule_id,
                        training_data_source, battery, input_digest
                    )
                    VALUES (%s, %s, %s, %s, %s, false, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(schema=sql.Identifier(self._derived)),
                (
                    marker.region,
                    marker.window.start,
                    marker.window.end,
                    ALL_TARIFFS,
                    Scenario.FORECAST_DRIVEN.value,
                    marker.reason,
                    DECISION_RULE_ID,
                    PRICE_PUBLICATION_RULE_ID,
                    SELECTION_RULE_ID,
                    training_data_source,
                    _battery_json(battery),
                    _not_simulated_digest(marker),
                ),
            )
        return ForwardWriteCounts(runs=1, days=0, hours=0, replaced=replaced)

    def _delete(
        self,
        cur: psycopg.Cursor[tuple[object, ...]],
        region: str,
        window: CoverageWindow,
        tariff_ids: Sequence[str] | None,
    ) -> int:
        """Drop a window's runs, and by cascade its days and schedule.

        ``tariff_ids`` of ``None`` means every tariff filed against this window, marker included.
        """
        scope = sql.SQL("")
        params: list[object] = [region, window.start, window.end]
        if tariff_ids is not None:
            scope = sql.SQL(" AND tariff_id = ANY(%s)")
            params.append(list(tariff_ids))
        cur.execute(
            sql.SQL(
                """
                DELETE FROM {schema}.forward_dispatch_runs
                WHERE region = %s AND window_start = %s AND window_end = %s{scope}
                """
            ).format(schema=sql.Identifier(self._derived), scope=scope),
            params,
        )
        return cur.rowcount if cur.rowcount > 0 else 0

    def clear_window(self, window: CoverageWindow, region: str, tariff_id: str) -> int:
        """Drop a previous simulation without writing a replacement.

        Needed because a window can stop being simulable -- the models it depended on may no longer
        fit after a config change -- and a stale set of rows claiming otherwise is worse than none.
        The forecasting store carries the same capability for the same reason.

        Scoped to the one tariff named, and to the marker that would contradict it: dropping a
        window's ``dynamic_2024`` rows must not silently take ``static_2024``'s with them.
        """
        with self._conn.transaction(), self._conn.cursor() as cur:
            return self._delete(cur, region, window, (tariff_id, ALL_TARIFFS))

    def _insert_run(
        self,
        cur: psycopg.Cursor[tuple[object, ...]],
        solution: ForwardSolution,
        result: DispatchResult,
        params: Jsonb,
        digest: str,
        solver_version: str,
        training_data_source: str,
    ) -> int:
        forecast_driven = result.scenario is Scenario.FORECAST_DRIVEN
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.forward_dispatch_runs (
                    region, window_start, window_end, tariff_id, scenario,
                    sim_start, sim_end, simulated_days, fallback_days,
                    is_simulated, not_simulated_reason,
                    solver, solver_version, status, terminal_value_ct_kwh,
                    terminal_soc_delta_kwh, terminal_discharge_efficiency,
                    soc_start_kwh, soc_end_kwh,
                    energy_cost_eur, feed_in_revenue_eur, net_cost_eur,
                    terminal_value_eur, objective_eur,
                    battery_charge_kwh, battery_discharge_kwh,
                    clipped_hours, priced_hours, expected_hours,
                    pv_model_key, load_model_key,
                    decision_rule_id, price_publication_rule_id, selection_rule_id,
                    training_data_source, battery, input_digest
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, NULL, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s)
                RETURNING id
                """
            ).format(schema=sql.Identifier(self._derived)),
            (
                solution.region,
                solution.window.start,
                solution.window.end,
                solution.tariff_id,
                result.scenario.value,
                solution.sim_start,
                solution.sim_end,
                solution.simulated_days,
                solution.fallback_days,
                result.solver,
                solver_version,
                result.status,
                solution.terminal_value_eur_kwh * 100.0,
                # Written verbatim: settlement already quantised the delta, and re-rounding either
                # here would break the identity the mart documents.
                result.terminal_soc_delta_kwh,
                result.terminal_discharge_efficiency,
                round_kwh(result.soc_start_kwh),
                round_kwh(result.soc_end_kwh),
                result.energy_cost_eur,
                result.feed_in_revenue_eur,
                result.net_cost_eur,
                result.terminal_value_eur,
                result.objective_eur,
                round_kwh(result.battery_charge_kwh),
                round_kwh(result.battery_discharge_kwh),
                # Only the executed scenario has a plan it could have been clipped away from; the
                # two references were never planned, so zero here is a fact rather than a default.
                solution.execution.clipped_hours if forecast_driven else 0,
                result.priced_hours,
                solution.expected_hours,
                solution.pv_model_key,
                solution.load_model_key,
                DECISION_RULE_ID,
                PRICE_PUBLICATION_RULE_ID,
                SELECTION_RULE_ID,
                training_data_source,
                params,
                digest,
            ),
        )
        row = cur.fetchone()
        assert row is not None  # RETURNING id on a successful INSERT always yields a row
        run_id = row[0]
        assert isinstance(run_id, int)
        return run_id

    def _insert_days(
        self,
        cur: psycopg.Cursor[tuple[object, ...]],
        solution: ForwardSolution,
        result: DispatchResult,
        run_id: int,
    ) -> int:
        """Per-day costing for every scenario, and the SoC chain for the executed one.

        The three reference scenarios are costed per day as well, from the same settled hours,
        because the README figure plots three cumulative differences against naive and needs all
        four curves to do it -- a mart that held only one would push that arithmetic into the
        plotting script. None of the three carries a per-day plan here: two were never planned, and
        the third's day outcomes are not kept, so ``plan_status`` says so rather than pretending the
        day was decided.
        """
        by_date = _hours_by_date(result.hours)
        outcomes = {day.local_date: day for day in solution.days}
        planned_scenario = result.scenario is Scenario.FORECAST_DRIVEN
        rows = []
        for local_date, hours in sorted(by_date.items()):
            outcome = outcomes[local_date] if planned_scenario else None
            decided = outcomes.get(local_date)
            rows.append(
                (
                    run_id,
                    solution.region,
                    solution.window.start,
                    solution.window.end,
                    solution.tariff_id,
                    result.scenario.value,
                    local_date,
                    _timestamp(decided.decision_ms) if decided is not None else None,
                    outcome.plan_status if outcome else PLAN_STATUS_NOT_PLANNED,
                    round_kwh(outcome.soc_start_kwh) if outcome else None,
                    round_kwh(outcome.soc_end_kwh) if outcome else None,
                    outcome.clipped_hours if outcome else 0,
                    outcome.clamped_forecast_hours if outcome else 0,
                    outcome.pv_fit_day if outcome else None,
                    outcome.load_fit_day if outcome else None,
                    _sum(hour.energy_cost_eur for hour in hours),
                    _sum(hour.feed_in_revenue_eur for hour in hours),
                    _net(hours),
                    sum(1 for hour in hours if hour.is_priced),
                )
            )
        cur.executemany(
            sql.SQL(
                """
                INSERT INTO {schema}.forward_dispatch_days (
                    run_id, region, window_start, window_end, tariff_id, scenario,
                    local_date, decision_time, plan_status,
                    soc_start_kwh, soc_end_kwh, clipped_hours, clamped_forecast_hours,
                    pv_fit_day, load_fit_day,
                    energy_cost_eur, feed_in_revenue_eur, net_cost_eur, priced_hours
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema=sql.Identifier(self._derived)),
            rows,
        )
        return len(rows)

    def _insert_schedule(
        self,
        cur: psycopg.Cursor[tuple[object, ...]],
        solution: ForwardSolution,
        result: DispatchResult,
        run_id: int,
    ) -> int:
        is_forecast_driven = result.scenario is Scenario.FORECAST_DRIVEN
        planned: Sequence[ExecutedHour | None] = (
            solution.execution.hours if is_forecast_driven else (None,) * len(result.hours)
        )
        context: Sequence[PlannedHourContext | None] = (
            solution.planned_context if is_forecast_driven else (None,) * len(result.hours)
        )
        rows = [
            (
                run_id,
                solution.region,
                solution.window.start,
                solution.window.end,
                solution.tariff_id,
                result.scenario.value,
                hour.ts_utc,
                hour.pv_production_kwh,
                hour.household_load_kwh,
                round_kwh(hour.battery_charge_kwh),
                round_kwh(hour.battery_discharge_kwh),
                round_kwh(hour.soc_kwh),
                round_kwh(hour.grid_import_kwh),
                round_kwh(hour.grid_export_kwh),
                hour.price_eur_mwh,
                hour.import_price_ct_kwh,
                hour.energy_cost_eur,
                hour.feed_in_revenue_eur,
                hour.is_priced,
                round_kwh(plan.planned_charge_kwh) if plan is not None else None,
                round_kwh(plan.planned_discharge_kwh) if plan is not None else None,
                plan.was_clipped if plan is not None else None,
                expected.pv_production_kwh if expected is not None else None,
                expected.household_load_kwh if expected is not None else None,
                round_kwh(expected.grid_import_kwh) if expected is not None else None,
                round_kwh(expected.grid_export_kwh) if expected is not None else None,
                round_kwh(expected.soc_kwh) if expected is not None else None,
            )
            for hour, plan, expected in zip(result.hours, planned, context, strict=True)
        ]
        cur.executemany(
            sql.SQL(
                """
                INSERT INTO {schema}.forward_dispatch_schedule (
                    run_id, region, window_start, window_end, tariff_id, scenario, ts_utc,
                    pv_production_kwh, household_load_kwh,
                    battery_charge_kwh, battery_discharge_kwh, soc_kwh,
                    grid_import_kwh, grid_export_kwh,
                    price_eur_mwh, import_price_ct_kwh,
                    energy_cost_eur, feed_in_revenue_eur, is_priced,
                    planned_battery_charge_kwh, planned_battery_discharge_kwh, was_clipped,
                    planned_pv_production_kwh, planned_household_load_kwh,
                    planned_grid_import_kwh, planned_grid_export_kwh, planned_soc_kwh
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema=sql.Identifier(self._derived)),
            rows,
        )
        return len(rows)


def _battery_json(battery: BatteryConfig) -> Jsonb:
    return Jsonb(
        {
            "capacity_kwh": battery.capacity_kwh,
            "soc_min": battery.soc_min,
            "soc_max": battery.soc_max,
            "max_charge_kw": battery.max_charge_kw,
            "max_discharge_kw": battery.max_discharge_kw,
            "round_trip_efficiency": battery.round_trip_efficiency,
        }
    )


def _input_digest(solution: ForwardSolution, battery: BatteryConfig) -> str:
    """A hash of what went *in* to the simulation, never of what came out.

    M6's reasoning, plus the two model keys and the simulated span: a run that differs only in which
    model planned it was given a different problem, and a digest that could not say so would leave
    the most likely cause of a divergence invisible. The plans themselves are excluded for the same
    reason the schedule is -- they are outputs.
    """
    payload = json.dumps(
        {
            "region": solution.region,
            "window": [solution.window.start.isoformat(), solution.window.end.isoformat()],
            "sim": [solution.sim_start.isoformat(), solution.sim_end.isoformat()],
            "tariff_id": solution.tariff_id,
            "terminal_value_eur_kwh": solution.terminal_value_eur_kwh,
            "pv_model_key": solution.pv_model_key,
            "load_model_key": solution.load_model_key,
            "battery": [
                battery.capacity_kwh,
                battery.soc_min,
                battery.soc_max,
                battery.max_charge_kw,
                battery.max_discharge_kw,
                battery.round_trip_efficiency,
            ],
            "hours": [
                [h.ts_utc.isoformat(), h.pv_production_kwh, h.household_load_kwh, h.price_eur_mwh]
                for h in solution.hours
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _not_simulated_digest(marker: NotSimulated) -> str:
    payload = json.dumps(
        {
            "region": marker.region,
            "window": [marker.window.start.isoformat(), marker.window.end.isoformat()],
            "reason": marker.reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _hours_by_date(hours: Sequence[DispatchHour]) -> dict[date, list[DispatchHour]]:
    grouped: dict[date, list[DispatchHour]] = {}
    for hour in hours:
        grouped.setdefault(local_date_of(hour.ts_utc), []).append(hour)
    return grouped


def _sum(values: Iterable[float | None]) -> float | None:
    """Total a day's priced money, or ``None`` if the day priced nothing.

    Not ``0.0``: a day with no priced hour did not cost nothing, it is unknown, and the daily chart
    must show a gap rather than a flat stretch. Same rule as ``settle_window``'s.
    """
    present = [value for value in values if value is not None]
    return round(sum(present), 6) if present else None


def _net(hours: Sequence[DispatchHour]) -> float | None:
    cost = _sum(hour.energy_cost_eur for hour in hours)
    revenue = _sum(hour.feed_in_revenue_eur for hour in hours)
    if cost is None and revenue is None:
        return None
    return round((cost or 0.0) - (revenue or 0.0), 6)


def _timestamp(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)
