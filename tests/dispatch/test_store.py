"""The derived zone against a real database: DDL, the input guard, and replace-on-rerun.

These are the tests that pin the *contract difference* from the raw zone. Ingestion is append-only
and content-hashed, and re-running it is a verifiable no-op. A dispatch solve is neither: the
optimal schedule is not unique, so a re-solve is a replacement rather than a revision worth keeping.
The tests below assert that replacing is what actually happens, because a store that quietly
appended would double every mart total on the second run.

Skips when no Postgres is reachable, so ``just test`` stays green without one; CI sets
``ENERGY_REQUIRE_POSTGRES`` and the shared fixture turns the skip into a failure there.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from energy_platform.config import BatteryConfig
from energy_platform.dispatch import runner
from energy_platform.dispatch.store import (
    DispatchInputError,
    DispatchRepository,
)
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.tariffs.catalog import load_catalog

pytestmark = pytest.mark.postgres

DERIVED_SCHEMA = "derived_test"
MARTS_SCHEMA = "marts_test"
REGION = "home"
# A June day: 24 hours, no DST transition, so the expected-hour arithmetic is not what is under
# test here (tests/dispatch/test_windows.py covers that).
WINDOW = CoverageWindow(start=date(2024, 6, 10), end=date(2024, 6, 10))
BATTERY = BatteryConfig()


@pytest.fixture
def dispatch_repo(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[DispatchRepository]:
    """A repository over throwaway schemas, with a stand-in energy mart, dropped on teardown."""
    with postgres_conn.cursor() as cur:
        for schema in (DERIVED_SCHEMA, MARTS_SCHEMA):
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(MARTS_SCHEMA)))
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE {}.mart_hourly_energy (
                    ts_utc                timestamptz NOT NULL,
                    region                text        NOT NULL,
                    local_date            date        NOT NULL,
                    pv_production_kwh     double precision,
                    household_load_kwh    double precision,
                    battery_charge_kwh    double precision,
                    battery_discharge_kwh double precision,
                    soc_frac              double precision,
                    grid_import_kwh       double precision,
                    grid_export_kwh       double precision,
                    price_eur_mwh         double precision
                )
                """
            ).format(sql.Identifier(MARTS_SCHEMA))
        )
    postgres_conn.commit()

    repo = DispatchRepository(
        postgres_conn, derived_schema=DERIVED_SCHEMA, marts_schema=MARTS_SCHEMA
    )
    repo.ensure_schema()
    try:
        yield repo
    finally:
        with postgres_conn.cursor() as cur:
            for schema in (DERIVED_SCHEMA, MARTS_SCHEMA):
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
        postgres_conn.commit()


def _seed_mart(
    conn: psycopg.Connection[tuple[object, ...]], hours: int = 24, region: str = REGION
) -> None:
    """A day of plausible telemetry: a midday PV hump, flat load, an evening price spike."""
    start = datetime(2024, 6, 9, 22, tzinfo=UTC)  # Berlin midnight on 2024-06-10 (CEST)
    rows = []
    for index in range(hours):
        ts = start + timedelta(hours=index)
        berlin_hour = (ts.hour + 2) % 24
        pv = 4.0 if 9 <= berlin_hour <= 15 else 0.0
        load = 1.0
        price = 300.0 if 18 <= berlin_hour <= 20 else 40.0
        rows.append(
            (
                ts,
                region,
                date(2024, 6, 10),
                pv,
                load,
                0.0,
                0.0,
                0.05,
                max(load - pv, 0.0),
                max(pv - load, 0.0),
                price,
            )
        )
    with conn.cursor() as cur:
        cur.executemany(
            sql.SQL(
                """
                INSERT INTO {}.mart_hourly_energy VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(sql.Identifier(MARTS_SCHEMA)),
            rows,
        )
    conn.commit()


def _solve(repo: DispatchRepository, tariff_id: str = "dynamic_2024") -> runner.WindowSolution:
    catalog = load_catalog()
    hours = repo.read_window(WINDOW, REGION)
    return runner.solve(WINDOW, REGION, hours, catalog[tariff_id], catalog["eeg_2024"], BATTERY)


def _as_float(value: object) -> float:
    assert isinstance(value, int | float | str)
    return float(value)


def _count(conn: psycopg.Connection[tuple[object, ...]], table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(DERIVED_SCHEMA), sql.Identifier(table)
            )
        )
        row = cur.fetchone()
        assert row is not None
        count = row[0]
        assert isinstance(count, int)
        return count


# -- Schema creation -------------------------------------------------------------------------


def test_ensure_schema_is_idempotent(dispatch_repo: DispatchRepository) -> None:
    """Run twice, because the CLI calls it on every invocation."""
    dispatch_repo.ensure_schema()
    dispatch_repo.ensure_schema()
    assert dispatch_repo.input_relation_exists()


# -- Reading the optimiser's inputs -----------------------------------------------------------


def test_it_reads_the_window_in_order_with_gaps_preserved(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    _seed_mart(postgres_conn)
    hours = dispatch_repo.read_window(WINDOW, REGION)

    assert len(hours) == WINDOW.expected_hours == 24
    assert [hour.ts_utc for hour in hours] == sorted(hour.ts_utc for hour in hours)
    assert all(hour.price_eur_mwh is not None for hour in hours)


def test_a_short_window_fails_loudly_instead_of_optimising_a_partial_week(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """The expectation is the Berlin calendar, never the row count -- so truncation cannot pass.

    A mart that lost six hours would otherwise be optimised as if it were the whole window, and the
    resulting cost reported against a full week's baseline.
    """
    _seed_mart(postgres_conn, hours=18)

    with pytest.raises(DispatchInputError, match="18 hours"):
        dispatch_repo.read_window(WINDOW, REGION)


def test_it_lists_only_the_sites_present_in_the_window(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    _seed_mart(postgres_conn)
    _seed_mart(postgres_conn, region="cottage")

    assert dispatch_repo.regions(WINDOW) == ("cottage", "home")


# -- Replace-on-rerun, not append -------------------------------------------------------------


def test_a_solve_writes_one_run_per_scenario_and_a_full_schedule(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    _seed_mart(postgres_conn)
    counts = dispatch_repo.replace_window(_solve(dispatch_repo), BATTERY, "test")

    assert counts.runs == 4
    assert counts.hours == 4 * 24
    assert counts.replaced == 0
    assert _count(postgres_conn, "dispatch_runs") == 4
    assert _count(postgres_conn, "dispatch_schedule") == 96


def test_re_solving_replaces_the_window_rather_than_appending_to_it(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """The contract difference from the raw zone, asserted.

    An optimal schedule is not unique, so a second solve is a replacement. Appending would leave two
    equally-valid answers in the table and double every total the mart aggregates.
    """
    _seed_mart(postgres_conn)
    dispatch_repo.replace_window(_solve(dispatch_repo), BATTERY, "test")
    second = dispatch_repo.replace_window(_solve(dispatch_repo), BATTERY, "test")

    assert second.replaced == 4
    assert _count(postgres_conn, "dispatch_runs") == 4
    assert _count(postgres_conn, "dispatch_schedule") == 96


def test_two_tariffs_coexist_in_the_same_window(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """Replacement is keyed on the tariff too, so solving one must not evict the other."""
    _seed_mart(postgres_conn)
    dispatch_repo.replace_window(_solve(dispatch_repo, "dynamic_2024"), BATTERY, "test")
    dispatch_repo.replace_window(_solve(dispatch_repo, "static_2024"), BATTERY, "test")

    assert _count(postgres_conn, "dispatch_runs") == 8
    with postgres_conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT tariff_id FROM {}.dispatch_runs ORDER BY 1").format(
                sql.Identifier(DERIVED_SCHEMA)
            )
        )
        assert [row[0] for row in cur.fetchall()] == ["dynamic_2024", "static_2024"]


def test_the_persisted_values_reproduce_the_result_they_came_from(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """Value-stable, which is the claim the module actually makes -- see its docstring.

    The schedule may differ between solver versions; the objective may not, and it is the objective
    every mart and every test reads.
    """
    _seed_mart(postgres_conn)
    solution = _solve(dispatch_repo)
    dispatch_repo.replace_window(solution, BATTERY, "test")

    expected = {result.scenario.value: result.objective_eur for result in solution.results}
    with postgres_conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT scenario, objective_eur FROM {}.dispatch_runs").format(
                sql.Identifier(DERIVED_SCHEMA)
            )
        )
        persisted = {str(row[0]): _as_float(row[1]) for row in cur.fetchall()}

    assert persisted == pytest.approx(expected)


def test_the_input_digest_is_stable_across_re_solves_of_the_same_problem(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """It hashes the *inputs*, so it answers 'was it given the same problem?' rather than 'did the
    solver pick the same vertex?' -- the only one of those two questions worth asking."""
    _seed_mart(postgres_conn)
    dispatch_repo.replace_window(_solve(dispatch_repo), BATTERY, "test")
    with postgres_conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT input_digest FROM {}.dispatch_runs").format(
                sql.Identifier(DERIVED_SCHEMA)
            )
        )
        first = [row[0] for row in cur.fetchall()]

    dispatch_repo.replace_window(_solve(dispatch_repo), BATTERY, "test")
    with postgres_conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT input_digest FROM {}.dispatch_runs").format(
                sql.Identifier(DERIVED_SCHEMA)
            )
        )
        second = [row[0] for row in cur.fetchall()]

    assert len(first) == 1
    assert first == second


def test_the_optimum_beats_the_continuous_baseline_on_real_shaped_data(
    dispatch_repo: DispatchRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """End to end through the store: an evening price spike is what the battery is for."""
    _seed_mart(postgres_conn)
    solution = _solve(dispatch_repo)
    objectives = {result.scenario.value: result.objective_eur for result in solution.results}

    assert objectives["optimal"] <= objectives["naive_continuous"] + 1e-6
    assert objectives["optimal"] <= objectives["no_battery"] + 1e-6
