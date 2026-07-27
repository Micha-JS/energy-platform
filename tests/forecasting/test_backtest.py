"""What the backtest reports, and what it must never quietly stop reporting.

``tests/forecasting/test_models.py`` pins the pieces; this pins the assembly. Four claims the
milestone makes in prose are asserted here instead, because each of them was true of the
documentation and false of the code at some point:

1. ``pvlib_hgb`` fits the **residual** against the physical chain. The provenance interlock in
   ``models.load_artifact`` refuses a synthetic-trained artifact *on the grounds that* it has
   learned to undo a transposition real data needs. If the model were fitted on levels that refusal
   would be a superstition, so the residual is asserted rather than described.
2. A window too short to train on reports its baselines. Emitting nothing instead makes the window
   vanish from ``mart_forecast_eval``, and ``assert_baselines_exist_for_every_model`` then passes
   over a window nobody can see is missing.
3. A vintage that stops short of the window does not abort the run. A gap is a gap -- NaN, not a
   short feature row and a ``ValueError`` out of ``build_matrix``.
4. A run reports the span it actually trained on. ``train_end`` equal to the last evaluated day is
   exactly the lookahead this milestone exists to disprove.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

import numpy as np
import pytest

from energy_platform.config import ForecastConfig, PvSystemConfig, Site
from energy_platform.forecasting import physical, runner
from energy_platform.forecasting.runner import (
    MODEL_LOAD_HGB,
    MODEL_PERSISTENCE,
    MODEL_PVLIB_HGB,
    MODEL_PVLIB_PHYSICAL,
    MODEL_SEASONAL_NAIVE,
    MODEL_TOY_PHYSICAL,
    TARGET_LOAD,
    TARGET_PV,
    BacktestResult,
    ModelRun,
)
from energy_platform.forecasting.store import VINTAGE_COLUMNS, Observation, VintageHour
from energy_platform.forecasting.vintage import issue_time_ms
from energy_platform.orchestration.ingest import berlin_day_window

SITE: Final = Site(id="home", latitude=52.52, longitude=13.40)
PV: Final = PvSystemConfig()
_HOUR_MS: Final = 3_600_000

# A lag of 0 is the real-telemetry setting (a house reporting over Home Assistant), and it keeps
# these fixtures a handful of days rather than the ~5 the synthetic archive delay forces.
CONFIG: Final = ForecastConfig(telemetry_lag_hours=0, min_train_days=4, fold_stride_days=2)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _hours(day: date) -> list[int]:
    window = berlin_day_window(day)
    return list(range(window.start_ms, window.end_ms, _HOUR_MS))


def _ghi(ts_utc_ms: int) -> float:
    """A smooth daily irradiance bell, deterministic in the instant. Zero outside daylight."""
    hour = (ts_utc_ms // _HOUR_MS) % 24
    elevation = math.sin(math.pi * (hour - 4) / 14.0)
    return round(650.0 * elevation, 6) if 4 < hour < 18 else 0.0


def _load_kwh(ts_utc_ms: int) -> float:
    hour = (ts_utc_ms // _HOUR_MS) % 24
    return round(0.25 + 0.2 * math.sin(math.pi * hour / 12.0) ** 2, 6)


def _observations(days: Sequence[date]) -> list[Observation]:
    """Hourly actuals whose PV is exactly ``GHI / 100`` -- see :func:`_perfect_physics`."""
    return [
        Observation(
            ts_utc_ms=ts_ms,
            local_date=day,
            pv_production_kwh=_ghi(ts_ms) / 100.0,
            household_load_kwh=_load_kwh(ts_ms),
        )
        for day in days
        for ts_ms in _hours(day)
    ]


def _vintages(
    days: Sequence[date], *, truncate_last_day_from_hour: int | None = None
) -> list[VintageHour]:
    """One vintage per target day, issued six hours before that day begins.

    ``truncate_last_day_from_hour`` drops the tail of the final day's coverage, reproducing the
    shipped seed data's shape: vintages backfilled to an issue day earlier than the window's end.
    """
    rows: list[VintageHour] = []
    for day in days:
        issue_ms = issue_time_ms(day) - 6 * _HOUR_MS
        for index, ts_ms in enumerate(_hours(day)):
            if (
                truncate_last_day_from_hour is not None
                and day == days[-1]
                and index >= truncate_last_day_from_hour
            ):
                continue
            ghi = _ghi(ts_ms)
            rows.append(
                VintageHour(
                    issue_ms=issue_ms,
                    target_ts_utc_ms=ts_ms,
                    values={
                        "ghi_w_m2": ghi,
                        "direct_radiation_w_m2": ghi * 0.68,
                        "diffuse_radiation_w_m2": ghi * 0.32,
                        "temperature_c": 14.0,
                        "cloud_cover_pct": 20.0,
                        "wind_speed_m_s": 3.2,
                    },
                )
            )
    return rows


def _run(
    target: str,
    start: date,
    end: date,
    *,
    config: ForecastConfig = CONFIG,
    truncate_last_day_from_hour: int | None = None,
) -> BacktestResult:
    days = _days(start, end)
    return runner.backtest(
        SITE,
        target,
        start,
        end,
        _observations(days),
        _vintages(days, truncate_last_day_from_hour=truncate_last_day_from_hour),
        config=config,
        pv=PV,
    )


def _by_key(result: BacktestResult) -> dict[str, ModelRun]:
    return {run.model_key: run for run in result.runs}


def _perfect_physics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the physical chain exactly right, so the residual it leaves is exactly zero.

    The discriminator: a model fitted on the residual has *nothing left to learn* and reproduces the
    truth to floating-point equality, while a model fitted on the level has to relearn the whole
    curve from the features and cannot. Patching the chain rather than tuning the fixture keeps the
    assertion about which quantity is fitted, not about how good pvlib happens to be.
    """
    monkeypatch.setattr(
        physical,
        "pvlib_ac_kwh",
        lambda ghi, direct, diffuse, temp, wind, geometry, pv: np.asarray(ghi) / 100.0,
    )


# -- 1. The residual, asserted rather than described --------------------------------------


def test_the_pv_model_fits_the_residual_against_the_physical_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _perfect_physics(monkeypatch)
    runs = _by_key(_run(TARGET_PV, date(2024, 4, 1), date(2024, 4, 12)))

    hybrid = runs[MODEL_PVLIB_HGB].predictions
    assert hybrid, "the hybrid must actually have been fitted"
    for prediction in hybrid:
        assert prediction.actual_kwh is not None
        assert prediction.p50_kwh == pytest.approx(prediction.actual_kwh, abs=1e-9)


def test_the_hybrid_adds_its_correction_back_onto_the_physical_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a zero residual the hybrid is the physical chain, hour for hour. It is not merely close.

    A level model trained on the same features would land near these values and pass a loose
    tolerance, which is why the two series are compared to each other rather than to a threshold.
    """
    _perfect_physics(monkeypatch)
    runs = _by_key(_run(TARGET_PV, date(2024, 4, 1), date(2024, 4, 12)))

    chain = {(p.ts_utc_ms, p.fold): p.p50_kwh for p in runs[MODEL_PVLIB_PHYSICAL].predictions}
    for prediction in runs[MODEL_PVLIB_HGB].predictions:
        assert prediction.p50_kwh == pytest.approx(chain[(prediction.ts_utc_ms, prediction.fold)])


# -- 2. A window too short to train on still reports something ----------------------------


def test_a_window_too_short_to_train_on_reports_its_baselines_and_no_model() -> None:
    """The case ``assert_baselines_exist_for_every_model`` documents as legitimate, exactly.

    Both halves are load-bearing. A window that emits *nothing* makes that dbt test pass vacuously:
    its scope CTE selects model rows and there are none to select. A window that also emits the
    physical models fails it for a true reason -- on seven days with no prior history
    ``seasonal_naive`` can never resolve an equivalent hour, so it scores nothing, drops out of the
    mart, and leaves a model row with no baseline beside it.
    """
    config = ForecastConfig(telemetry_lag_hours=0, min_train_days=28, fold_stride_days=7)
    result = _run(TARGET_PV, date(2024, 3, 25), date(2024, 3, 31), config=config)
    runs = _by_key(result)

    assert set(runs) == {MODEL_PERSISTENCE, MODEL_SEASONAL_NAIVE}
    # 7 days, one of them the 23-hour spring DST day.
    assert len(runs[MODEL_PERSISTENCE].predictions) == 7 * 24 - 1
    assert not result.skipped_days


def test_a_short_window_reports_no_training_span_for_models_that_train_on_nothing() -> None:
    config = ForecastConfig(telemetry_lag_hours=0, min_train_days=28, fold_stride_days=7)
    run = _by_key(_run(TARGET_LOAD, date(2024, 4, 1), date(2024, 4, 7), config=config))[
        MODEL_PERSISTENCE
    ]
    assert (run.train_start, run.train_end, run.n_train_rows) == (None, None, 0)


# -- 3. A vintage that stops short is a gap, not a crash ----------------------------------


def test_a_vintage_that_stops_short_of_the_window_does_not_abort_the_backtest() -> None:
    """The shipped seed data's shape: vintages backfilled to an issue day before the window ends.

    The uncovered hours must come back as no prediction. Before this was fixed they came back as a
    feature row six columns narrower than its neighbours and took the whole run down with it.
    """
    # Chosen so the truncated day *is* a fold: with min_train_days=4 and a stride of 2, day index
    # 10 is evaluated. The shipped seed data escaped this only because its uncovered tail landed
    # between folds -- a stride of 1 would have hit it.
    end = date(2024, 4, 11)
    result = _run(TARGET_PV, date(2024, 4, 1), end, truncate_last_day_from_hour=18)
    runs = _by_key(result)

    uncovered = [
        prediction
        for prediction in runs[MODEL_PVLIB_PHYSICAL].predictions
        if prediction.local_date == end and prediction.horizon_hour >= 18
    ]
    assert uncovered, "the truncated day must still be evaluated"
    assert all(prediction.p50_kwh is None for prediction in uncovered)
    assert MODEL_PVLIB_HGB in runs, "one truncated day must not cost the whole window its model"


def test_a_gap_in_the_vintage_never_shortens_a_feature_row() -> None:
    """The structural half: the column set comes from the declared tuple, not from the data."""
    result = _run(TARGET_PV, date(2024, 4, 1), date(2024, 4, 12), truncate_last_day_from_hour=18)
    names = _by_key(result)[MODEL_PVLIB_HGB].feature_names
    assert {f"fc_{column}" for column in VINTAGE_COLUMNS} <= set(names)


# -- 4. The training span is the span trained on ------------------------------------------


def test_a_run_reports_the_span_it_trained_on_rather_than_the_window() -> None:
    """``train_end == window_end`` would be the leakage this milestone exists to disprove."""
    start, end = date(2024, 4, 1), date(2024, 4, 12)
    run = _by_key(_run(TARGET_LOAD, start, end))[MODEL_LOAD_HGB]

    assert run.train_start == start
    assert run.train_end is not None
    assert run.train_end < end
    assert run.n_train_rows > 0


def test_the_training_free_models_carry_no_training_span_and_no_row_count() -> None:
    """A reader of ``derived.forecast_runs`` must not see 2 000 training rows against a lookup."""
    runs = _by_key(_run(TARGET_PV, date(2024, 4, 1), date(2024, 4, 12)))
    for key in (MODEL_PERSISTENCE, MODEL_SEASONAL_NAIVE, MODEL_TOY_PHYSICAL, MODEL_PVLIB_PHYSICAL):
        run = runs[key]
        assert (run.train_start, run.train_end, run.n_train_rows) == (None, None, 0), key
    assert runs[MODEL_PVLIB_HGB].n_train_rows > 0


def test_every_model_is_scored_over_the_same_days() -> None:
    """The like-for-like claim: one evaluated day set per window, shared by every model in it."""
    runs = _by_key(_run(TARGET_PV, date(2024, 4, 1), date(2024, 4, 12)))
    day_sets = {
        key: {prediction.local_date for prediction in run.predictions} for key, run in runs.items()
    }
    assert len(set(map(frozenset, day_sets.values()))) == 1, day_sets
