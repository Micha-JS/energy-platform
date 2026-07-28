"""The derived zone: reading the optimiser's inputs, persisting its results.

**This is the first component whose results the raw zone's contract deliberately does not cover,
and the difference is worth being explicit about.**

Ingestion is bit-reproducible. A partition's content hash is a function of the bytes the source
returned, re-running it writes nothing, and that no-op is asserted on every CI run. That contract
is the whole point of the raw zone, and it works because there is exactly one right answer.

Optimisation has no such property. The optimal *value* is unique -- it is the minimum -- but the
optimal *schedule* frequently is not: two hours at the same price are interchangeable, and any
number of dispatches achieve the same cost. Re-solving after a solver upgrade may legitimately
return a different schedule with an identical objective. Content-hashing that would report a
revision where nothing changed, and treating a re-solve as an append would accumulate equivalent
answers forever.

So the derived zone is **replace-on-rerun**: solving a (site, window, tariff) deletes its previous
rows and writes the new ones in one transaction. The claim made about it is:

* **Value-stable** -- cost and objective columns reproduce to the persisted rounding for a pinned
  solver version, which is what every test and mart asserts on.
* **Not hash-guaranteed** -- no claim is made that a schedule is byte-identical across platforms or
  solver versions, and nothing downstream may assume one. ``solver`` / ``solver_version`` /
  ``input_digest`` are recorded on every run so a divergence can be attributed rather than argued
  about.

One invariant does not cover a whole platform, and scoping it honestly is better than stretching it
until it is not true.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.model import DispatchResult, HourInputs, round_eur, round_kwh
from energy_platform.dispatch.runner import WindowSolution
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.rows import as_float

# The mart the optimiser reads. A dbt table, not the raw zone: DST-correct calendar columns, the
# declared spine, and the price join by explicit bidding zone all live in the dbt layer, and
# re-deriving any of that here would be a second implementation of the thing M4 exists to own.
INPUT_RELATION = "mart_hourly_energy"


class DispatchInputError(RuntimeError):
    """Raised when the warehouse cannot supply the optimiser's inputs, with what to run instead."""


def _ddl(schema: str) -> list[sql.Composed]:
    ident = sql.Identifier(schema)
    return [
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.dispatch_runs (
                id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                region                  text        NOT NULL,
                window_start            date        NOT NULL,
                window_end              date        NOT NULL,
                tariff_id               text        NOT NULL,
                scenario                text        NOT NULL,
                solver                  text        NOT NULL,
                solver_version          text        NOT NULL,
                status                  text        NOT NULL,
                terminal_value_ct_kwh   double precision NOT NULL,
                -- The other two factors of the terminal credit. Nullable, and deliberately so:
                -- they were added after the table existed, the ALTER below is what puts them on an
                -- existing one, and a NOT NULL ALTER cannot run against rows that predate them. The
                -- not-nullness is asserted where it is observable -- as tests on
                -- mart_dispatch_comparison -- and is transient by construction, because one
                -- `just dispatch` replace-writes every declared window.
                terminal_soc_delta_kwh        double precision,
                terminal_discharge_efficiency double precision,
                soc_start_kwh           double precision NOT NULL,
                soc_end_kwh             double precision NOT NULL,
                energy_cost_eur         double precision NOT NULL,
                feed_in_revenue_eur     double precision NOT NULL,
                net_cost_eur            double precision NOT NULL,
                terminal_value_eur      double precision NOT NULL,
                objective_eur           double precision NOT NULL,
                battery_charge_kwh      double precision NOT NULL,
                battery_discharge_kwh   double precision NOT NULL,
                priced_hours            integer     NOT NULL,
                expected_hours          integer     NOT NULL,
                battery                 jsonb       NOT NULL,
                input_digest            text        NOT NULL,
                solved_at               timestamptz NOT NULL DEFAULT now()
            )
            """
        ).format(schema=ident),
        # Migration for a table created before these two columns existed. `CREATE TABLE IF NOT
        # EXISTS` above is a no-op on an existing table, so without these a pre-change derived zone
        # would keep failing the mart's not_null tests with no way forward short of dropping it.
        sql.SQL(
            """
            ALTER TABLE {schema}.dispatch_runs
                ADD COLUMN IF NOT EXISTS terminal_soc_delta_kwh double precision,
                ADD COLUMN IF NOT EXISTS terminal_discharge_efficiency double precision
            """
        ).format(schema=ident),
        # One row per scenario per (site, window, tariff). UNIQUE rather than a plain index: the
        # replace-write below relies on it, and a duplicate here would double every mart total.
        sql.SQL(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS dispatch_runs_key
            ON {schema}.dispatch_runs (region, window_start, window_end, tariff_id, scenario)
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.dispatch_schedule (
                run_id                bigint NOT NULL
                    REFERENCES {schema}.dispatch_runs (id) ON DELETE CASCADE,
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
                is_priced             boolean     NOT NULL
            )
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE INDEX IF NOT EXISTS dispatch_schedule_run_idx
            ON {schema}.dispatch_schedule (run_id)
            """
        ).format(schema=ident),
    ]


@dataclass(frozen=True, slots=True)
class WriteCounts:
    """What one replace-write did, so the CLI can report it and a test can assert on it."""

    runs: int
    hours: int
    replaced: int


class DispatchRepository:
    """Reads the optimiser's inputs from the marts, writes its results to the derived zone."""

    def __init__(
        self,
        conn: psycopg.Connection[tuple[object, ...]],
        *,
        derived_schema: str,
        marts_schema: str,
    ) -> None:
        self._conn = conn
        self._derived = derived_schema
        self._marts = marts_schema

    def ensure_schema(self) -> None:
        """Create the derived zone if it is not there. Idempotent, like the raw zone's."""
        with self._conn.cursor() as cur:
            for statement in _ddl(self._derived):
                cur.execute(statement)
        self._conn.commit()

    def regions(self, window: CoverageWindow) -> tuple[str, ...]:
        """The sites the mart holds telemetry for in this window, so none is silently skipped."""
        query = sql.SQL(
            """
            SELECT DISTINCT region
            FROM {relation}
            WHERE local_date BETWEEN %s AND %s
            ORDER BY region
            """
        ).format(relation=self._input_relation())
        with self._conn.cursor() as cur:
            cur.execute(query, (window.start, window.end))
            return tuple(str(row[0]) for row in cur.fetchall())

    def read_window(self, window: CoverageWindow, region: str) -> tuple[HourInputs, ...]:
        """Hourly inputs for one site over one window, ordered, gaps preserved as ``None``.

        Filtering on ``local_date`` rather than on ``ts_utc`` keeps the window's edges on Berlin
        midnights, which is what makes the row count match ``CoverageWindow.expected_hours`` across
        a DST transition instead of missing (or doubling) an hour.
        """
        query = sql.SQL(
            """
            SELECT ts_utc, pv_production_kwh, household_load_kwh, price_eur_mwh,
                   battery_charge_kwh, battery_discharge_kwh, grid_import_kwh, grid_export_kwh,
                   soc_frac
            FROM {relation}
            WHERE region = %s AND local_date BETWEEN %s AND %s
            ORDER BY ts_utc
            """
        ).format(relation=self._input_relation())
        with self._conn.cursor() as cur:
            cur.execute(query, (region, window.start, window.end))
            rows = cur.fetchall()

        hours = tuple(
            HourInputs(
                ts_utc=_as_datetime(row[0]),
                pv_production_kwh=as_float(row[1]),
                household_load_kwh=as_float(row[2]),
                price_eur_mwh=as_float(row[3]),
                battery_charge_kwh=as_float(row[4]),
                battery_discharge_kwh=as_float(row[5]),
                grid_import_kwh=as_float(row[6]),
                grid_export_kwh=as_float(row[7]),
                soc_frac=as_float(row[8]),
            )
            for row in rows
        )
        # The declared window is the expectation, never the row count itself: a truncated mart must
        # fail here rather than silently optimise a shorter week and report it as a full one.
        if len(hours) != window.expected_hours:
            raise DispatchInputError(
                f"{self._marts}.{INPUT_RELATION} has {len(hours)} hours for {region} over "
                f"{window}, but the Berlin calendar says {window.expected_hours}; rebuild the "
                "warehouse (`just dbt-seed && just dbt-build`) before optimising"
            )
        return hours

    def replace_window(
        self, solution: WindowSolution, battery: BatteryConfig, solver_version: str
    ) -> WriteCounts:
        """Persist every scenario for one (site, window, tariff), replacing any previous solve.

        Delete-then-insert inside a single transaction: a re-solve supersedes its predecessor
        outright rather than appending a revision, because two schedules with the same objective are
        the same answer and keeping both would say otherwise. See the module docstring.
        """
        digest = _input_digest(solution, battery)
        params = Jsonb(
            {
                "capacity_kwh": battery.capacity_kwh,
                "soc_min": battery.soc_min,
                "soc_max": battery.soc_max,
                "max_charge_kw": battery.max_charge_kw,
                "max_discharge_kw": battery.max_discharge_kw,
                "round_trip_efficiency": battery.round_trip_efficiency,
            }
        )
        key = (
            solution.region,
            solution.window.start,
            solution.window.end,
            solution.tariff_id,
        )

        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    DELETE FROM {schema}.dispatch_runs
                    WHERE region = %s AND window_start = %s AND window_end = %s AND tariff_id = %s
                    """
                ).format(schema=sql.Identifier(self._derived)),
                key,
            )
            replaced = cur.rowcount if cur.rowcount > 0 else 0

            hours_written = 0
            for result in solution.results:
                run_id = self._insert_run(cur, solution, result, params, digest, solver_version)
                hours_written += self._insert_schedule(cur, solution, result, run_id)

        return WriteCounts(runs=len(solution.results), hours=hours_written, replaced=replaced)

    def _insert_run(
        self,
        cur: psycopg.Cursor[tuple[object, ...]],
        solution: WindowSolution,
        result: DispatchResult,
        params: Jsonb,
        digest: str,
        solver_version: str,
    ) -> int:
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.dispatch_runs (
                    region, window_start, window_end, tariff_id, scenario,
                    solver, solver_version, status, terminal_value_ct_kwh,
                    terminal_soc_delta_kwh, terminal_discharge_efficiency,
                    soc_start_kwh, soc_end_kwh,
                    energy_cost_eur, feed_in_revenue_eur, net_cost_eur,
                    terminal_value_eur, objective_eur,
                    battery_charge_kwh, battery_discharge_kwh,
                    priced_hours, expected_hours, battery, input_digest
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s)
                RETURNING id
                """
            ).format(schema=sql.Identifier(self._derived)),
            (
                solution.region,
                solution.window.start,
                solution.window.end,
                solution.tariff_id,
                result.scenario.value,
                result.solver,
                solver_version,
                result.status,
                solution.terminal_value_eur_kwh * 100.0,
                # Both written verbatim: settlement already quantised the delta, and re-rounding
                # either here would break the identity the mart documents.
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
                result.priced_hours,
                solution.expected_hours,
                params,
                digest,
            ),
        )
        row = cur.fetchone()
        assert row is not None  # RETURNING id on a successful INSERT always yields a row
        run_id = row[0]
        assert isinstance(run_id, int)
        return run_id

    def _insert_schedule(
        self,
        cur: psycopg.Cursor[tuple[object, ...]],
        solution: WindowSolution,
        result: DispatchResult,
        run_id: int,
    ) -> int:
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
            )
            for hour in result.hours
        ]
        cur.executemany(
            sql.SQL(
                """
                INSERT INTO {schema}.dispatch_schedule (
                    run_id, region, window_start, window_end, tariff_id, scenario, ts_utc,
                    pv_production_kwh, household_load_kwh,
                    battery_charge_kwh, battery_discharge_kwh, soc_kwh,
                    grid_import_kwh, grid_export_kwh,
                    price_eur_mwh, import_price_ct_kwh,
                    energy_cost_eur, feed_in_revenue_eur, is_priced
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema=sql.Identifier(self._derived)),
            rows,
        )
        return len(rows)

    def _input_relation(self) -> sql.Composed:
        return sql.SQL("{schema}.{table}").format(
            schema=sql.Identifier(self._marts), table=sql.Identifier(INPUT_RELATION)
        )

    def input_relation_exists(self) -> bool:
        """Whether the mart the optimiser reads has been built."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{self._marts}.{INPUT_RELATION}",))
            row = cur.fetchone()
            return row is not None and row[0] is not None


def _input_digest(solution: WindowSolution, battery: BatteryConfig) -> str:
    """A hash of what went *in* to the solve -- inputs, physics, tariff, terminal valuation.

    Deliberately not a hash of the results. The schedule is not unique, so hashing it would report
    a change where none of substance occurred; hashing the inputs answers the question actually
    worth asking of a differing run -- *was it given the same problem?*
    """
    payload = json.dumps(
        {
            "region": solution.region,
            "window": [solution.window.start.isoformat(), solution.window.end.isoformat()],
            "tariff_id": solution.tariff_id,
            "terminal_value_eur_kwh": solution.terminal_value_eur_kwh,
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


def _as_datetime(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def scenario_totals(results: Sequence[DispatchResult]) -> dict[str, float | None]:
    """Objective per scenario, for logging a solved window in one line."""
    return {result.scenario.value: round_eur(result.objective_eur) for result in results}
