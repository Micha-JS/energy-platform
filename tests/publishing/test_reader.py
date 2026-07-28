"""Reading a plan out of the derived layer, against a real Postgres.

Marked ``postgres`` like the raw-zone tests: the SQL here does DST-correct Berlin-day bucketing and
a three-way join, neither of which a stub could vouch for. CI sets ``ENERGY_REQUIRE_POSTGRES=1`` so
a missing database fails rather than skips.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from energy_platform.publishing.reader import ForwardPlanReader, PlanNotAvailableError

pytestmark = pytest.mark.postgres

SCHEMA = "derived_publish_test"
SITE = "home"
TARIFF = "dynamic_2024"
DAY = date(2024, 6, 12)


@pytest.fixture
def derived(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A throwaway derived schema holding one run, one day and its hours."""
    with postgres_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE {}.forward_dispatch_runs (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    region text, window_start date, window_end date, tariff_id text,
                    scenario text, is_simulated boolean,
                    input_digest text, pv_model_key text, load_model_key text,
                    decision_rule_id text, price_publication_rule_id text,
                    selection_rule_id text, training_data_source text,
                    solver text, solver_version text
                )
                """
            ).format(sql.Identifier(SCHEMA))
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE {}.forward_dispatch_days (
                    run_id bigint, region text, tariff_id text, scenario text,
                    local_date date, decision_time timestamptz, plan_status text
                )
                """
            ).format(sql.Identifier(SCHEMA))
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE {}.forward_dispatch_schedule (
                    run_id bigint, region text, tariff_id text, scenario text, ts_utc timestamptz,
                    planned_battery_charge_kwh double precision,
                    planned_battery_discharge_kwh double precision,
                    planned_grid_import_kwh double precision,
                    planned_grid_export_kwh double precision,
                    planned_soc_kwh double precision,
                    planned_pv_production_kwh double precision,
                    planned_household_load_kwh double precision,
                    import_price_ct_kwh double precision
                )
                """
            ).format(sql.Identifier(SCHEMA))
        )
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {}.forward_dispatch_runs (
                    region, window_start, window_end, tariff_id, scenario, is_simulated,
                    input_digest, pv_model_key, load_model_key, decision_rule_id,
                    price_publication_rule_id, selection_rule_id, training_data_source,
                    solver, solver_version
                ) VALUES (%s, %s, %s, %s, 'forecast_driven', true,
                          'digest-1', 'pv_v1', 'load_v1', 'berlin_midnight_before_target_day',
                          'day_ahead_auction_d_minus_1_1245_berlin', 'latest_vintage',
                          'synthetic', 'HiGHS', '1.7')
                RETURNING id
                """
            ).format(sql.Identifier(SCHEMA)),
            (SITE, date(2024, 6, 1), date(2024, 6, 30), TARIFF),
        )
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {}.forward_dispatch_days
                (run_id, region, tariff_id, scenario, local_date, decision_time, plan_status)
                VALUES (%s, %s, %s, 'forecast_driven', %s, %s, 'planned')
                """
            ).format(sql.Identifier(SCHEMA)),
            (run_id, SITE, TARIFF, DAY, datetime(2024, 6, 11, 22, 0, tzinfo=UTC)),
        )
        # 24 Berlin hours starting at 2024-06-11T22:00Z (Berlin midnight in CEST). The 4th hour is
        # left unresolved so the null-versus-zero rule has something to bite on.
        start = datetime(2024, 6, 11, 22, 0, tzinfo=UTC)
        for index in range(24):
            resolved = index != 3
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.forward_dispatch_schedule VALUES
                    (%s, %s, %s, 'forecast_driven', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(sql.Identifier(SCHEMA)),
                (
                    run_id,
                    SITE,
                    TARIFF,
                    start + timedelta(hours=index),
                    0.0,
                    0.0,
                    1.0 if resolved else None,
                    0.0 if resolved else None,
                    7.0 if resolved else None,
                    0.0 if resolved else None,
                    1.0 if resolved else None,
                    30.0,
                ),
            )
    postgres_conn.commit()
    yield postgres_conn
    with postgres_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(SCHEMA)))
    postgres_conn.commit()


def test_reads_the_days_hours_in_order(derived: psycopg.Connection[tuple[object, ...]]) -> None:
    plan = ForwardPlanReader(derived, derived_schema=SCHEMA).read_day(SITE, DAY, TARIFF)
    assert len(plan.hours) == 24
    assert plan.hours[0].ts_utc == "2024-06-11T22:00:00Z"
    assert [h.ts_utc for h in plan.hours] == sorted(h.ts_utc for h in plan.hours)
    assert plan.plan_status == "planned"


def test_an_unresolved_hour_carries_no_instruction(
    derived: psycopg.Connection[tuple[object, ...]],
) -> None:
    """The stored 0.0 hold must not reach the wire as an instruction.

    The warehouse legitimately records a hold for an hour the solve could not resolve. Published
    verbatim it tells a house to do something specific on an hour nobody planned -- so the reader
    keys on the expectation columns, which are null exactly when the hour was not resolved.
    """
    plan = ForwardPlanReader(derived, derived_schema=SCHEMA).read_day(SITE, DAY, TARIFF)
    unresolved = plan.hours[3]
    assert unresolved.battery_charge_kwh is None
    assert unresolved.expected_soc_kwh is None
    # The price is a property of the hour, not of the plan: the auction cleared regardless.
    assert unresolved.import_price_ct_kwh == 30.0
    assert plan.coverage.planned_hours == 23
    assert plan.coverage.expected_hours == 24


def test_provenance_comes_off_the_run(derived: psycopg.Connection[tuple[object, ...]]) -> None:
    plan = ForwardPlanReader(derived, derived_schema=SCHEMA).read_day(SITE, DAY, TARIFF)
    assert plan.provenance.input_digest == "digest-1"
    assert plan.provenance.training_data_source == "synthetic"
    assert plan.provenance.solver_version == "1.7"


def test_an_unknown_tariff_refuses_and_names_the_fix(
    derived: psycopg.Connection[tuple[object, ...]],
) -> None:
    """A day is planned once per tariff, so this is genuinely ambiguous and must not be guessed.

    Publishing the plan for a tariff the household is not on would be well-formed, plausible, and
    wrong -- the worst combination for something a house acts on.
    """
    reader = ForwardPlanReader(derived, derived_schema=SCHEMA)
    with pytest.raises(PlanNotAvailableError, match="ENERGY_TARIFF_ID"):
        reader.read_day(SITE, DAY, "some_other_tariff")


def test_a_day_outside_the_simulation_refuses(
    derived: psycopg.Connection[tuple[object, ...]],
) -> None:
    reader = ForwardPlanReader(derived, derived_schema=SCHEMA)
    with pytest.raises(PlanNotAvailableError, match="no simulated"):
        reader.read_day(SITE, date(2024, 7, 1), TARIFF)


def test_an_unbuilt_warehouse_says_so_rather_than_reporting_no_plan(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    # Distinguishing "M8 never ran" from "no plan for that day" is the difference between
    # `just warehouse` and a genuine coverage gap.
    reader = ForwardPlanReader(postgres_conn, derived_schema="schema_that_does_not_exist")
    with pytest.raises(PlanNotAvailableError, match="just warehouse"):
        reader.read_day(SITE, DAY, TARIFF)
