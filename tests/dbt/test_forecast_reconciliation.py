"""The forecast metric arithmetic is written twice, and this pins the two together.

``mart_forecast_eval`` computes MAE and pinball loss in SQL so the definitions are auditable in the
dbt docs site; ``energy_platform.forecasting.models`` computes pinball loss in Python for the models
themselves. Two implementations of one formula drift, so every mart row is recomputed here from the
raw predictions -- the same treatment the tariff and dispatch arithmetic already get
(``test_tariff_reconciliation``, ``test_dispatch_reconciliation``).

Also pinned here: the two claims about the mart that a reader would otherwise have to take on
trust -- that ``role`` marks the oracle rather than leaving it to be ranked as a competitor, and
that no gap was ever scored as a zero.

Skipped when the warehouse is absent so the general ``pytest`` run stays green; CI sets
``ENERGY_REQUIRE_DBT=1`` and the skip becomes a failure.
"""

from __future__ import annotations

import os
from collections import defaultdict

import psycopg
import pytest

from energy_platform.config import PostgresConfig
from energy_platform.forecasting.models import pinball_loss
from energy_platform.rows import as_float, as_required_float
from tests.dbt.warehouse_guard import skip_or_fail

pytestmark = pytest.mark.postgres

# The mart rounds to 6 dp, so a stored value carries up to +/-5e-7. Recomputing from the unrounded
# predictions can therefore differ by that much -- and the half-the-MAE identity compares two
# independently rounded columns, where mae/2 contributes a further +/-2.5e-7. 1e-6 covers both with
# room to spare and is still three orders of magnitude below a Wh.
_TOLERANCE = 1e-6


@pytest.fixture
def warehouse_conn() -> psycopg.Connection[tuple[object, ...]]:
    config = PostgresConfig.from_env()
    try:
        conn = psycopg.connect(config.dsn, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("ENERGY_REQUIRE_POSTGRES"):
            pytest.fail(f"ENERGY_REQUIRE_POSTGRES is set but no Postgres is reachable ({exc})")
        pytest.skip(f"no Postgres available ({exc})")
    return conn


def _require_rows(
    conn: psycopg.Connection[tuple[object, ...]], query: str
) -> list[tuple[object, ...]]:
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall()
    except psycopg.errors.UndefinedTable:
        skip_or_fail("mart_forecast_eval is missing; run `just warehouse` first")


def test_every_mart_row_reproduces_from_its_own_predictions(
    warehouse_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Recompute MAE and all three pinball losses per (model, horizon hour) and compare."""
    config = PostgresConfig.from_env()
    rows = _require_rows(
        warehouse_conn,
        f"""
        select r.site, r.target, r.model_key, r.window_start, r.window_end, p.horizon_hour,
               p.p10_kwh, p.p50_kwh, p.p90_kwh, p.actual_kwh
        from {config.derived_schema}.forecast_runs r
        join {config.derived_schema}.forecast_predictions p on p.run_id = r.id
        where p.p50_kwh is not null and p.actual_kwh is not null
        """,
    )
    if not rows:
        skip_or_fail("no scoreable forecast predictions; run `just warehouse` first")

    recomputed: dict[tuple[object, ...], list[tuple[float, float, float, float]]] = defaultdict(
        list
    )
    for row in rows:
        key = tuple(row[:6])
        p10, p50, p90 = as_float(row[6]), as_required_float(row[7]), as_float(row[8])
        actual = as_required_float(row[9])
        recomputed[key].append(
            (
                abs(p50 - actual),
                float("nan") if p10 is None else pinball_loss(actual, p10, 0.10),
                pinball_loss(actual, p50, 0.50),
                float("nan") if p90 is None else pinball_loss(actual, p90, 0.90),
            )
        )

    mart = _require_rows(
        warehouse_conn,
        f"""
        select site, target, model_key, window_start, window_end, horizon_hour,
               mae_kwh, pinball_p10_kwh, pinball_p50_kwh, pinball_p90_kwh, scored_hours
        from {config.marts_schema}.mart_forecast_eval
        """,
    )
    assert mart, "mart_forecast_eval is empty but predictions exist"

    for row in mart:
        key = tuple(row[:6])
        expected = recomputed[key]
        assert expected, f"mart row {key} has no predictions behind it"
        assert int(as_required_float(row[10])) == len(expected), f"scored_hours wrong for {key}"

        mae = as_required_float(row[6])
        assert mae == pytest.approx(sum(e[0] for e in expected) / len(expected), abs=_TOLERANCE), (
            f"MAE disagrees for {key}"
        )

        for offset, index in ((7, 1), (8, 2), (9, 3)):
            stored = as_float(row[offset])
            values = [e[index] for e in expected]
            if any(value != value for value in values):  # a NaN means no interval was emitted
                assert stored is None, f"quantile loss stored for a point forecast at {key}"
                continue
            assert stored is not None
            assert stored == pytest.approx(sum(values) / len(values), abs=_TOLERANCE), (
                f"pinball loss column {offset} disagrees for {key}"
            )


def test_pinball_at_the_median_is_half_the_mae(
    warehouse_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """An identity, not a coincidence: 0.5 * |error| either side of the median.

    Worth asserting against the built mart rather than in a unit test, because it catches a whole
    class of SQL error -- a sign flip or a swapped branch in the CASE -- that a unit test of the
    Python helper would sail past.
    """
    config = PostgresConfig.from_env()
    rows = _require_rows(
        warehouse_conn,
        f"select mae_kwh, pinball_p50_kwh from {config.marts_schema}.mart_forecast_eval",
    )
    if not rows:
        skip_or_fail("mart_forecast_eval is empty; run `just warehouse` first")
    for mae, pinball in rows:
        assert as_required_float(pinball) == pytest.approx(
            as_required_float(mae) / 2, abs=_TOLERANCE
        )


def test_the_oracle_is_labelled_rather_than_left_to_be_ranked(
    warehouse_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """On synthetic windows the flat-plate PV model generated the labels, so it must carry a role.

    This is the structural half of the honesty framing: without it the claim lives only in a README
    paragraph, and a chart built by ordering on mae_kwh would silently crown the data-generating
    process.
    """
    config = PostgresConfig.from_env()
    rows = _require_rows(
        warehouse_conn,
        f"""
        select distinct model_key, role, training_data_source
        from {config.marts_schema}.mart_forecast_eval
        """,
    )
    if not rows:
        skip_or_fail("mart_forecast_eval is empty; run `just warehouse` first")

    by_model = {str(model): (str(role), str(source)) for model, role, source in rows}
    if "toy_physical" in by_model:
        role, source = by_model["toy_physical"]
        expected = "oracle" if source == "synthetic" else "model"
        assert role == expected, (
            f"toy_physical is role={role!r} on {source} data; on synthetic data it IS the "
            "data-generating process and must be labelled oracle"
        )
    for baseline in ("persistence", "seasonal_naive"):
        if baseline in by_model:
            assert by_model[baseline][0] == "baseline"


def test_no_gap_was_scored_as_a_zero(
    warehouse_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """`scored_hours` must count only hours with both a prediction and an actual.

    NaN over fabrication, at the last step before the answer: counting a missing hour as a
    zero-error hour would flatter exactly the models that failed to predict it.
    """
    config = PostgresConfig.from_env()
    rows = _require_rows(
        warehouse_conn,
        f"""
        select coalesce(sum(m.scored_hours), 0), (
            select count(*)
            from {config.derived_schema}.forecast_predictions p
            where p.p50_kwh is null or p.actual_kwh is null
        )
        from {config.marts_schema}.mart_forecast_eval m
        """,
    )
    scored, unscoreable = int(as_required_float(rows[0][0])), int(as_required_float(rows[0][1]))
    total = _require_rows(
        warehouse_conn,
        f"select count(*) from {config.derived_schema}.forecast_predictions",
    )
    assert scored + unscoreable == int(as_required_float(total[0][0])), (
        "scored + unscoreable hours do not account for every prediction row"
    )
