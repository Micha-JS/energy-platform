"""Guard: the Python tariff engine and the dbt tariff macro compute the same money.

Tariff *parameters* cannot drift -- there is one catalogue CSV, which dbt seeds and
:mod:`energy_platform.tariffs.catalog` reads directly. Tariff *arithmetic* is necessarily
written twice, once in Python (M6's optimiser evaluates cost inside its objective and cannot
call SQL per candidate dispatch) and once in the ``tariff_import_price_ct_kwh`` macro. This is
what stops the two from diverging: every priced hour the warehouse built is recomputed here
with the Python engine and compared.

A factor-of-ten unit slip, VAT applied on one side only, or a sign error in one layer fails
here and names the hour and tariff that diverged.

The comparison uses a tolerance rather than bit equality on purpose: dbt computes in exact
decimal and casts to double, Python computes in float throughout, so the two agree to within a
rounding step. The tolerance is a hundred-millionth of a cent -- far below any real error and
far above float dust.

Skipped when the warehouse has not been built, so ``just test`` stays green without one. CI
sets ``ENERGY_REQUIRE_DBT=1`` in the dbt job, which turns that skip into a failure -- a
reconciliation that silently does not run is indistinguishable from one that passes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import psycopg
import pytest

from energy_platform.rows import as_float
from energy_platform.tariffs.catalog import TariffKind, TariffSpec, load_catalog
from energy_platform.tariffs.engine import (
    energy_cost_eur,
    feed_in_revenue_eur,
    import_price_ct_kwh,
)
from tests.dbt.warehouse_guard import skip_or_fail

pytestmark = pytest.mark.postgres

# dbt writes marts to <target schema>_<layer>; the intermediate layer is where the hourly money
# lives, which is the grain a divergence is diagnosable at.
COST_RELATION = "analytics_intermediate.int_hourly_tariff_cost"

# EUR. Nine decimal places is a hundred-millionth of a cent: below any arithmetic error worth
# having and above the last-bit difference between exact-decimal and float evaluation.
TOLERANCE_EUR = 1e-9


@pytest.fixture
def cost_rows(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[list[dict[str, Any]]]:
    """Every row of the hourly tariff-cost model, as dicts keyed by column name."""
    with postgres_conn.cursor() as cur:
        cur.execute("select to_regclass(%s)", (COST_RELATION,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            skip_or_fail(f"{COST_RELATION} does not exist; run `dbt build` first")

        cur.execute(
            f"""
            select ts_utc, region, scenario, tariff_id,
                   price_eur_mwh, grid_import_kwh, grid_export_kwh,
                   import_price_ct_kwh, energy_cost_eur, feed_in_revenue_eur, is_priced
            from {COST_RELATION}
            order by ts_utc, region, scenario, tariff_id
            """  # COST_RELATION is a module constant, never user input
        )
        columns = [d[0] for d in cur.description or ()]
        yield [dict(zip(columns, values, strict=True)) for values in cur.fetchall()]


@pytest.fixture
def catalog() -> Mapping[str, TariffSpec]:
    return load_catalog()


def _feed_in_spec(catalog: Mapping[str, TariffSpec]) -> TariffSpec:
    feed_in = [s for s in catalog.values() if s.kind is TariffKind.FEED_IN]
    assert len(feed_in) == 1, f"expected exactly one feed_in row, found {len(feed_in)}"
    return feed_in[0]


def test_the_warehouse_priced_something(cost_rows: list[dict[str, Any]]) -> None:
    """An empty model would make every comparison below pass by silence."""
    assert cost_rows, f"{COST_RELATION} is empty -- nothing to reconcile"
    assert any(row["is_priced"] for row in cost_rows), "no hour in the warehouse was priced"


def test_both_scenarios_and_every_consumption_tariff_are_present(
    cost_rows: list[dict[str, Any]], catalog: Mapping[str, TariffSpec]
) -> None:
    """Catches a seed that failed to load or a filter that quietly dropped a tariff."""
    assert {row["scenario"] for row in cost_rows} == {"battery", "no_battery"}
    expected = {spec.tariff_id for spec in catalog.values() if spec.is_consumption}
    assert {row["tariff_id"] for row in cost_rows} == expected


def test_import_price_matches_the_python_engine(
    cost_rows: list[dict[str, Any]], catalog: Mapping[str, TariffSpec]
) -> None:
    """The per-kWh rate: where a factor-of-ten or a VAT slip shows up first."""
    for row in cost_rows:
        spec = catalog[row["tariff_id"]]
        expected = import_price_ct_kwh(spec, as_float(row["price_eur_mwh"]))
        actual = as_float(row["import_price_ct_kwh"])
        assert actual == pytest.approx(expected, abs=TOLERANCE_EUR, nan_ok=False), (
            f"import price diverged at {row['ts_utc']} {row['region']} "
            f"{row['scenario']} {row['tariff_id']}: SQL {actual}, Python {expected}"
        )


def test_energy_cost_matches_the_python_engine(
    cost_rows: list[dict[str, Any]], catalog: Mapping[str, TariffSpec]
) -> None:
    for row in cost_rows:
        spec = catalog[row["tariff_id"]]
        expected = energy_cost_eur(
            spec, as_float(row["grid_import_kwh"]), as_float(row["price_eur_mwh"])
        )
        actual = as_float(row["energy_cost_eur"])
        assert actual == pytest.approx(expected, abs=TOLERANCE_EUR, nan_ok=False), (
            f"energy cost diverged at {row['ts_utc']} {row['region']} "
            f"{row['scenario']} {row['tariff_id']}: SQL {actual}, Python {expected}"
        )


def test_feed_in_revenue_matches_the_python_engine(
    cost_rows: list[dict[str, Any]], catalog: Mapping[str, TariffSpec]
) -> None:
    """Also pins that the warehouse used the flat rate and not the spot price."""
    spec = _feed_in_spec(catalog)
    for row in cost_rows:
        expected = feed_in_revenue_eur(spec, as_float(row["grid_export_kwh"]))
        actual = as_float(row["feed_in_revenue_eur"])
        assert actual == pytest.approx(expected, abs=TOLERANCE_EUR, nan_ok=False), (
            f"feed-in revenue diverged at {row['ts_utc']} {row['region']} "
            f"{row['scenario']} {row['tariff_id']}: SQL {actual}, Python {expected}"
        )


def test_unpriced_hours_have_no_money_on_either_side(cost_rows: list[dict[str, Any]]) -> None:
    """is_priced is what the marts aggregate on, so a false positive understates every total."""
    for row in cost_rows:
        if row["is_priced"]:
            assert row["energy_cost_eur"] is not None
            assert row["feed_in_revenue_eur"] is not None
        else:
            missing = (
                row["energy_cost_eur"] is None
                or row["feed_in_revenue_eur"] is None
                or row["import_price_ct_kwh"] is None
            )
            assert missing, (
                f"{row['ts_utc']} {row['tariff_id']} is marked unpriced but carries a full "
                "set of money columns"
            )
