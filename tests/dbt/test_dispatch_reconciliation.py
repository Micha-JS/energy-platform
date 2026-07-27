"""Guard: M6's optimiser and M5's marts price the same hours the same way.

Two of the four dispatch scenarios are not new physics at all -- they are quantities the economics
layer already computes:

    dispatch `no_battery`         == int_hourly_tariff_cost `no_battery`   (PV only)
    dispatch `naive_telemetered`  == int_hourly_tariff_cost `battery`      (what M3 emitted)

They exist in the dispatch tables so that all four scenarios are settled by one code path and a
difference between their totals is a difference in dispatch rather than in accounting. The cost of
that is a second implementation of two things the warehouse already knew, and this is what stops the
two from drifting: every priced hour of both scenarios is compared against the SQL the marts built.

Without it, a savings figure could quietly be measured against a baseline that the rest of the
warehouse disagrees with -- and the disagreement would show up nowhere, because each layer is
internally consistent.

The optimiser's own scenarios (`naive_continuous`, `optimal`) have no SQL twin to reconcile against;
they are covered by the property tests in tests/dispatch/, which assert the invariants rather than
compare implementations.

Skipped when the warehouse has not been built or the optimiser has not run, so ``just test`` stays
green without either. CI sets ``ENERGY_REQUIRE_DBT=1``, which turns those skips into failures.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from energy_platform.rows import as_float
from tests.dbt.warehouse_guard import skip_or_fail

pytestmark = pytest.mark.postgres

COST_RELATION = "analytics_intermediate.int_hourly_tariff_cost"
SCHEDULE_RELATION = "derived.dispatch_schedule"

# EUR / kWh / ct-per-kWh. The dispatch schedule is settled to a millionth of a euro at its
# persistence boundary while the SQL keeps full double precision, so the two agree to within one
# rounding step. Anything larger is a real divergence, not a representation difference.
#
# Deliberately tighter than assert_optimal_never_costs_more_than_naive's 1e-5, and not an
# inconsistency: this compares one rounded value against its unrounded twin, hour by hour, so one
# step is the whole budget. That test compares a *difference* of two objectives, each of which is
# three independently rounded terms.
TOLERANCE = 1e-6

# dispatch scenario -> the M5 scenario that must equal it.
EQUIVALENT_SCENARIOS = {
    "no_battery": "no_battery",
    "naive_telemetered": "battery",
}


@pytest.fixture
def paired_rows(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[list[dict[str, Any]]]:
    """Every priced hour of both layers, joined on (hour, site, tariff, scenario)."""
    with postgres_conn.cursor() as cur:
        for relation in (COST_RELATION, SCHEDULE_RELATION):
            cur.execute("select to_regclass(%s)", (relation,))
            row = cur.fetchone()
            if row is None or row[0] is None:
                skip_or_fail(
                    f"{relation} does not exist; run `just warehouse` (dbt build -> dispatch -> "
                    "dbt build) first"
                )

        cur.execute(
            f"""
            select d.ts_utc, d.region, d.tariff_id, d.scenario as dispatch_scenario,
                   c.scenario as sql_scenario,
                   d.grid_import_kwh      as d_import,   c.grid_import_kwh      as c_import,
                   d.grid_export_kwh      as d_export,   c.grid_export_kwh      as c_export,
                   d.import_price_ct_kwh  as d_price,    c.import_price_ct_kwh  as c_price,
                   d.energy_cost_eur      as d_cost,     c.energy_cost_eur      as c_cost,
                   d.feed_in_revenue_eur  as d_revenue,  c.feed_in_revenue_eur  as c_revenue,
                   d.is_priced            as d_priced,   c.is_priced            as c_priced
            from {SCHEDULE_RELATION} d
            join {COST_RELATION} c
              on c.ts_utc = d.ts_utc
             and c.region = d.region
             and c.tariff_id = d.tariff_id
             and c.scenario = case d.scenario
                                  when 'no_battery' then 'no_battery'
                                  when 'naive_telemetered' then 'battery'
                              end
            where d.scenario in ('no_battery', 'naive_telemetered')
            order by d.ts_utc, d.region, d.tariff_id, d.scenario
            """  # both relations are module constants, never user input
        )
        columns = [d[0] for d in cur.description or ()]
        yield [dict(zip(columns, values, strict=True)) for values in cur.fetchall()]


def test_the_two_layers_actually_have_hours_in_common(paired_rows: list[dict[str, Any]]) -> None:
    """An empty join would make every comparison below pass by silence."""
    assert paired_rows, (
        f"no hour of {SCHEDULE_RELATION} joined to {COST_RELATION} -- the optimiser and the "
        "economics marts describe disjoint periods, which is itself the bug"
    )
    assert {row["dispatch_scenario"] for row in paired_rows} == set(EQUIVALENT_SCENARIOS)


def test_both_reconcilable_scenarios_cover_every_declared_hour(
    paired_rows: list[dict[str, Any]], postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """Every hour the optimiser wrote must find a partner: a partial join hides divergence."""
    with postgres_conn.cursor() as cur:
        cur.execute(
            f"""
            select count(*) from {SCHEDULE_RELATION}
            where scenario in ('no_battery', 'naive_telemetered')
            """  # module constant
        )
        row = cur.fetchone()
        assert row is not None
        written = row[0]

    assert len(paired_rows) == written, (
        f"{written} dispatch rows but only {len(paired_rows)} matched an economics row; the "
        "unmatched hours are exactly where the layers disagree about what exists"
    )


@pytest.mark.parametrize(
    ("dispatch_column", "sql_column", "what"),
    [
        ("d_import", "c_import", "grid import"),
        ("d_export", "c_export", "grid export"),
        ("d_price", "c_price", "import price"),
        ("d_cost", "c_cost", "energy cost"),
        ("d_revenue", "c_revenue", "feed-in revenue"),
    ],
)
def test_the_layers_agree_hour_by_hour(
    paired_rows: list[dict[str, Any]], dispatch_column: str, sql_column: str, what: str
) -> None:
    """A sign error, a unit slip, or a different counterfactual definition fails here by name."""
    for row in paired_rows:
        dispatch = as_float(row[dispatch_column])
        warehouse = as_float(row[sql_column])
        assert dispatch == pytest.approx(warehouse, abs=TOLERANCE, nan_ok=False), (
            f"{what} diverged at {row['ts_utc']} {row['region']} {row['tariff_id']} "
            f"{row['dispatch_scenario']}: dispatch {dispatch}, warehouse {warehouse}"
        )


def test_both_layers_agree_on_which_hours_are_priced(
    paired_rows: list[dict[str, Any]],
) -> None:
    """`is_priced` is what both layers aggregate on, so a disagreement skews every total.

    M5 additionally requires ``self_consumed_kwh``, which the dispatch layer does not carry -- but
    it is derived from load and import, so an hour priced by one and not the other means the two
    disagree about what data exists, not merely about what to do with it.
    """
    for row in paired_rows:
        assert row["d_priced"] == row["c_priced"], (
            f"{row['ts_utc']} {row['tariff_id']} {row['dispatch_scenario']}: dispatch says "
            f"is_priced={row['d_priced']}, the warehouse says {row['c_priced']}"
        )


def test_unpriced_hours_carry_no_money_on_either_side(
    paired_rows: list[dict[str, Any]],
) -> None:
    """NaN over fabrication, checked across the layer boundary as well as within each layer."""
    for row in paired_rows:
        if row["d_priced"]:
            assert row["d_cost"] is not None
            assert row["d_revenue"] is not None
        else:
            assert row["d_cost"] is None
            assert row["d_revenue"] is None
