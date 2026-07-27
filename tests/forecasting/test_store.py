"""The forecast derived zone against a real database: what a re-run replaces, and what it clears.

``replace_window`` argues at length that its delete is keyed on the *window* rather than on
``(window, model_key)``, so a model the harness stops emitting cannot linger in ``derived`` and go
on appearing in ``mart_forecast_eval`` beside models that were actually re-run. That reasoning has a
limit the code originally fell through: the delete keys were derived from the rows being *written*,
so a re-run that produced nothing for a window -- no eligible vintage left, no observations at all
-- deleted nothing, and the previous fit stayed on the shelf looking current.

So the scope is passed in rather than inferred, and these tests pin both halves: a window that
re-runs with fewer models loses the missing ones, and a window that re-runs with no models at all
loses everything. The second is the one that regressed, and it is the one a smoke test cannot see:
the failure mode is stale rows that look exactly like fresh ones.

Skips when no Postgres is reachable, so ``just test`` stays green without one; CI sets
``ENERGY_REQUIRE_POSTGRES`` and the shared fixture turns the skip into a failure there.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import psycopg
import pytest
from psycopg import sql

from energy_platform.forecasting.store import ForecastInputError, ForecastRepository

pytestmark = pytest.mark.postgres

DERIVED_SCHEMA = "derived_forecast_test"
MARTS_SCHEMA = "marts_forecast_test"
STAGING_SCHEMA = "staging_forecast_test"

SITE = "home"
TARGET = "household_load_kwh"
WINDOW = (date(2024, 6, 10), date(2024, 6, 16))
SCOPE = [(SITE, TARGET, *WINDOW)]


@pytest.fixture
def forecast_repo(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[ForecastRepository]:
    """A repository over a throwaway derived schema, dropped on teardown."""
    schemas = (DERIVED_SCHEMA, MARTS_SCHEMA, STAGING_SCHEMA)
    with postgres_conn.cursor() as cur:
        for schema in schemas:
            cur.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))
    postgres_conn.commit()

    repo = ForecastRepository(
        postgres_conn,
        derived_schema=DERIVED_SCHEMA,
        marts_schema=MARTS_SCHEMA,
        staging_schema=STAGING_SCHEMA,
    )
    repo.ensure_schema()
    try:
        yield repo
    finally:
        with postgres_conn.cursor() as cur:
            for schema in schemas:
                cur.execute(
                    sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema))
                )
        postgres_conn.commit()


def _run(model_key: str, *, hours: int = 3) -> tuple[dict[str, object], list[dict[str, object]]]:
    """One run header and a few prediction rows, shaped as ``store.run_payload`` produces them."""
    header: dict[str, object] = {
        "site": SITE,
        "target": TARGET,
        "model_key": model_key,
        "role": "model",
        "window_start": WINDOW[0],
        "window_end": WINDOW[1],
        "train_start": WINDOW[0],
        "train_end": WINDOW[1],
        "config_hash": f"hash_{model_key}",
        "selection_rule_id": "selection",
        "persistence_rule_id": "persistence",
        "telemetry_lag_hours": 120,
        "training_data_source": "synthetic",
        "forecast_source": "open_meteo",
        "seed": 0,
        "n_train_rows": 48,
        "feature_names": ["local_hour"],
        "hyperparameters": {"max_iter": 200},
        "library_versions": {"numpy": "2.0.0"},
        "artifact_key": None,
    }
    base_ms = 1_718_006_400_000  # 2024-06-10T08:00Z, inside the window
    predictions: list[dict[str, object]] = [
        {
            "ts_utc_ms": base_ms + hour * 3_600_000,
            "local_date": WINDOW[0],
            "horizon_hour": hour,
            "issue_ms": base_ms,
            "vintage_issue_ms": base_ms - 3_600_000,
            "fold": 0,
            "p10_kwh": 0.1,
            "p50_kwh": 0.2,
            "p90_kwh": 0.3,
            "actual_kwh": 0.25,
        }
        for hour in range(hours)
    ]
    return header, predictions


def _stored(conn: psycopg.Connection[tuple[object, ...]]) -> set[str]:
    """The model keys actually on the shelf -- read back, never inferred from the write's counts."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("select model_key from {}.forecast_runs").format(sql.Identifier(DERIVED_SCHEMA))
        )
        return {str(row[0]) for row in cur.fetchall()}


def test_a_rerun_replaces_rather_than_appends(
    forecast_repo: ForecastRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """The baseline contract, and the reason the unique index exists."""
    first = forecast_repo.replace_window(SCOPE, [_run("load_hgb")])
    second = forecast_repo.replace_window(SCOPE, [_run("load_hgb")])

    assert (first.runs, first.replaced) == (1, 0)
    assert (second.runs, second.replaced) == (1, 1)
    assert _stored(postgres_conn) == {"load_hgb"}


def test_a_model_the_harness_stops_emitting_loses_its_rows(
    forecast_repo: ForecastRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """Keyed on the window, not on the model -- the case the delete was written for."""
    forecast_repo.replace_window(SCOPE, [_run("load_hgb"), _run("persistence")])
    forecast_repo.replace_window(SCOPE, [_run("persistence")])

    assert _stored(postgres_conn) == {"persistence"}


def test_a_window_that_now_produces_nothing_is_cleared_rather_than_left_standing(
    forecast_repo: ForecastRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """The regression. An empty re-run is a real instruction, not a no-op to skip.

    Shorten the vintage backfill (or drop the telemetry) and every day of a window becomes
    ineligible, so the backtest emits no runs at all. Deriving the delete keys from those runs
    deletes nothing, and ``mart_forecast_eval`` keeps serving the previous fit as current -- stale
    rows that are indistinguishable from fresh ones by inspection.
    """
    forecast_repo.replace_window(SCOPE, [_run("load_hgb"), _run("persistence")])
    assert _stored(postgres_conn)

    counts = forecast_repo.replace_window(SCOPE, [])

    assert (counts.runs, counts.predictions, counts.replaced) == (0, 0, 2)
    assert _stored(postgres_conn) == set()


def test_a_run_outside_the_declared_scope_is_refused(
    forecast_repo: ForecastRepository, postgres_conn: psycopg.Connection[tuple[object, ...]]
) -> None:
    """A run written under a key that was never cleared would sit beside what it meant to replace.

    The scope is what the caller evaluated. Letting a run land outside it would reintroduce the
    append-instead-of-replace failure through the back door, one key at a time.
    """
    other_window = [(SITE, "pv_production_kwh", *WINDOW)]
    with pytest.raises(ForecastInputError, match="outside the declared scope"):
        forecast_repo.replace_window(other_window, [_run("load_hgb")])

    assert _stored(postgres_conn) == set()
