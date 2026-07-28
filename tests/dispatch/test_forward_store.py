"""The forward-dispatch derived zone against a real database: two write paths over one key space.

``forward_dispatch_runs`` holds two mutually exclusive claims about a window -- "here is what four
scenarios cost under this tariff" and "nothing could be simulated here, and this is why" -- and
files the second under the sentinel ``tariff_id = '*'``. That sentinel is a value in the *same*
column as the real tariff ids, not a separate partition, so each write path has to clear the
other's rows or a window that changed state keeps both.

The direction that matters is not hypothetical: ``just forecast-reset`` or a raised
``min_train_days`` turns a simulated window unsimulable, and the models warming up turns it back.
Nothing downstream can catch the residue -- ``assert_forward_dispatch_windows_are_declared``,
``assert_simulated_span_is_inside_its_window`` and ``report_regret.py --check`` all still pass over
a stale row, because a stale row looks exactly like a fresh one. That is what these tests are for.

Skips when no Postgres is reachable, exactly as the M6 store tests next door do.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date

import psycopg
import pytest
from psycopg import sql

from energy_platform.dispatch import forward
from energy_platform.dispatch.forward_store import ALL_TARIFFS, ForwardDispatchRepository
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.rows import as_required_float
from energy_platform.tariffs.catalog import TariffSpec
from tests.dispatch.conftest import (
    BARE_DYNAMIC,
    FEED_IN,
    LOSSLESS,
    berlin_days,
    serving_plan,
    simulated_hours,
)

pytestmark = pytest.mark.postgres

DERIVED_SCHEMA = "derived_forward_test"
REGION = "home"
OTHER_TARIFF = replace(BARE_DYNAMIC, tariff_id="bare_dynamic_two")
DAYS = berlin_days(2)
WINDOW = CoverageWindow(DAYS[0], DAYS[-1])


@pytest.fixture
def forward_repo(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[ForwardDispatchRepository]:
    """A repository over a throwaway derived schema, dropped on teardown."""
    with postgres_conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(DERIVED_SCHEMA))
        )
    postgres_conn.commit()

    repo = ForwardDispatchRepository(postgres_conn, derived_schema=DERIVED_SCHEMA)
    repo.ensure_schema()
    try:
        yield repo
    finally:
        with postgres_conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(DERIVED_SCHEMA))
            )
        postgres_conn.commit()


def _solution(tariff: TariffSpec = BARE_DYNAMIC) -> forward.ForwardSolution:
    """A real two-day simulation, so the rows under test are the ones the CLI would write."""
    hours = simulated_hours(DAYS)
    solved = forward.simulate(
        WINDOW,
        REGION,
        hours,
        tariff,
        FEED_IN,
        LOSSLESS,
        pv_plan=serving_plan("pv_production_kwh", hours, DAYS),
        load_plan=serving_plan("household_load_kwh", hours, DAYS),
    )
    assert isinstance(solved, forward.ForwardSolution)
    return solved


def _marker() -> forward.NotSimulated:
    return forward.NotSimulated(WINDOW, REGION, forward.REASON_NO_FITTED_MODEL)


def _write(
    repo: ForwardDispatchRepository, what: forward.ForwardSolution | forward.NotSimulated
) -> None:
    repo.replace_window(what, LOSSLESS, "test", "synthetic")


def _filed_under(
    conn: psycopg.Connection[tuple[object, ...]],
) -> dict[str, int]:
    """How many run rows each tariff id currently claims for the window."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT tariff_id, count(*)
                FROM {}.forward_dispatch_runs
                WHERE region = %s AND window_start = %s AND window_end = %s
                GROUP BY tariff_id
                """
            ).format(sql.Identifier(DERIVED_SCHEMA)),
            (REGION, WINDOW.start, WINDOW.end),
        )
        return {str(tariff): int(as_required_float(count)) for tariff, count in cur.fetchall()}


def test_a_marker_supersedes_every_tariff_it_contradicts(
    forward_repo: ForwardDispatchRepository,
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """A marker is a claim about the models, so it is a claim about every tariff at once.

    Leaving the per-tariff rows behind would have mart_dispatch_regret reporting a previous run's
    costs as current, beside a marker saying the window was never simulated.
    """
    _write(forward_repo, _solution(BARE_DYNAMIC))
    _write(forward_repo, _solution(OTHER_TARIFF))
    assert set(_filed_under(postgres_conn)) == {"bare_dynamic", "bare_dynamic_two"}

    _write(forward_repo, _marker())
    assert _filed_under(postgres_conn) == {ALL_TARIFFS: 1}


def test_a_simulation_supersedes_the_marker_it_replaces(
    forward_repo: ForwardDispatchRepository,
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """The reverse trip: the models warm up and the window becomes simulable again.

    A surviving '*' row would be a phantom window sitting beside the real one -- is_simulated
    false, null costs, and no tariff it belongs to -- for every downstream reader to trip over.
    """
    _write(forward_repo, _marker())
    assert _filed_under(postgres_conn) == {ALL_TARIFFS: 1}

    _write(forward_repo, _solution(BARE_DYNAMIC))
    assert _filed_under(postgres_conn) == {"bare_dynamic": 4}


def test_replacing_one_tariff_leaves_the_others_untouched(
    forward_repo: ForwardDispatchRepository,
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """The marker's reach must not become a general one: a tariff still replaces only itself.

    The CLI loops tariffs inside the window, so a delete that took the whole window with it would
    leave only the last tariff simulated -- the opposite failure, and a silent one.
    """
    _write(forward_repo, _solution(BARE_DYNAMIC))
    _write(forward_repo, _solution(OTHER_TARIFF))
    _write(forward_repo, _solution(BARE_DYNAMIC))

    assert _filed_under(postgres_conn) == {"bare_dynamic": 4, "bare_dynamic_two": 4}


def test_clearing_a_window_takes_the_marker_that_contradicts_it(
    forward_repo: ForwardDispatchRepository,
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """``clear_window`` drops one tariff and the marker, and nobody else's rows."""
    _write(forward_repo, _solution(OTHER_TARIFF))
    _write(forward_repo, _solution(BARE_DYNAMIC))

    assert forward_repo.clear_window(WINDOW, REGION, "bare_dynamic") == 4
    assert _filed_under(postgres_conn) == {"bare_dynamic_two": 4}


def test_a_window_is_replaced_only_for_the_dates_it_declares(
    forward_repo: ForwardDispatchRepository,
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Neither path may reach past its own window -- the delete is keyed on the dates too."""
    _write(forward_repo, _solution(BARE_DYNAMIC))
    elsewhere = CoverageWindow(date(2023, 1, 1), date(2023, 1, 2))
    _write(forward_repo, replace(_solution(BARE_DYNAMIC), window=elsewhere))
    _write(forward_repo, _marker())

    with postgres_conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT count(*)
                FROM {}.forward_dispatch_runs
                WHERE window_start = %s
                """
            ).format(sql.Identifier(DERIVED_SCHEMA)),
            (elsewhere.start,),
        )
        row = cur.fetchone()
    assert row is not None
    assert as_required_float(row[0]) == 4
