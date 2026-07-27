"""The backtest: expanding-window CV over declared coverage, producing rows for the eval mart.

A pure function of already-loaded data, like ``dispatch.runner.solve`` -- it never opens a
connection, which is what makes it testable without Postgres and what makes the CLI and the Dagster
asset thin wrappers over the same core rather than two implementations.

**Folds are weekly, and that is the honest granularity rather than a shortcut.** With a five-day
observation lag, consecutive daily folds differ by one training day and produce near-identical
models; daily folds over the seeded windows would mean ~60 test days x 3 quantiles x 2 targets =
360 fits for information a ninth of that delivers. Weekly folds are stated here and recorded per
row, so the choice is visible in the warehouse rather than buried in a loop bound.

**Every model sees the same information set.** Baselines, the two physical models and the
gradient-boosted models are all resolved against the same issue instant, the same selected vintage
and the same observation lag. That is what makes the mart a like-for-like table; without it the
comparison would be measuring differences in what each model was allowed to look at.

**Roles, not just names.** Each model row carries a role: ``oracle`` for M3's flat-plate model on
synthetic windows -- where it is the data-generating process rather than a competitor and wins by
tautology -- ``baseline`` for the naive two, and ``model`` for everything else. Ranking by MAE
without filtering on role is then visibly wrong rather than quietly wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

import numpy as np
from numpy.typing import NDArray

from energy_platform.config import (
    TELEMETRY_SOURCE_SYNTHETIC,
    ForecastConfig,
    PvSystemConfig,
    Site,
)
from energy_platform.forecasting import baselines, physical
from energy_platform.forecasting.features import (
    Feature,
    FeatureKind,
    FeatureRow,
    build_matrix,
    calendar_features,
    lagged_actual_feature,
    vintage_features,
)
from energy_platform.forecasting.models import (
    HYPERPARAMETERS,
    QUANTILES,
    ModelCard,
    fit_quantile_forecaster,
    library_versions,
)
from energy_platform.forecasting.store import VINTAGE_COLUMNS, Observation, VintageHour
from energy_platform.forecasting.vintage import (
    SELECTION_RULE_ID,
    issue_time_ms,
    select_vintage,
)

TARGET_PV: Final = "pv_production_kwh"
TARGET_LOAD: Final = "household_load_kwh"
TARGETS: Final[tuple[str, ...]] = (TARGET_PV, TARGET_LOAD)

ROLE_ORACLE: Final = "oracle"
ROLE_BASELINE: Final = "baseline"
ROLE_MODEL: Final = "model"

MODEL_PERSISTENCE: Final = "persistence"
MODEL_SEASONAL_NAIVE: Final = "seasonal_naive"
MODEL_TOY_PHYSICAL: Final = "toy_physical"
MODEL_PVLIB_PHYSICAL: Final = "pvlib_physical"
MODEL_PVLIB_HGB: Final = "pvlib_hgb"
MODEL_LOAD_HGB: Final = "load_hgb"

# Models that are pure functions of the information set and fit nothing. Their runs carry a null
# training span and zero training rows: stamping the boosted models' span onto `persistence` would
# have `derived.forecast_runs` report ~2 000 training rows against a model that looks up one number.
TRAINING_FREE_MODELS: Final[frozenset[str]] = frozenset(
    {MODEL_PERSISTENCE, MODEL_SEASONAL_NAIVE, MODEL_TOY_PHYSICAL, MODEL_PVLIB_PHYSICAL}
)

_HOUR_MS: Final = 3_600_000


@dataclass(frozen=True, slots=True)
class Prediction:
    """One predicted hour, as it lands in ``derived.forecast_predictions``."""

    ts_utc_ms: int
    local_date: date
    horizon_hour: int
    issue_ms: int
    vintage_issue_ms: int | None
    fold: int
    p10_kwh: float | None
    p50_kwh: float | None
    p90_kwh: float | None
    actual_kwh: float | None


@dataclass(frozen=True, slots=True)
class ModelRun:
    """One (site, target, model, window) and the predictions behind it."""

    site: str
    target: str
    model_key: str
    role: str
    window_start: date
    window_end: date
    train_start: date | None
    train_end: date | None
    config_hash: str
    n_train_rows: int
    feature_names: tuple[str, ...]
    predictions: tuple[Prediction, ...]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    runs: tuple[ModelRun, ...]
    skipped_days: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class _DayData:
    """Everything a target day needs, built once whether or not the day becomes a fold."""

    rows: tuple[FeatureRow, ...]
    actuals: tuple[float | None, ...]
    vintage_ms: int
    toy_kwh: tuple[float | None, ...] | None
    pvlib_kwh: tuple[float | None, ...] | None


@dataclass(frozen=True, slots=True)
class _TrainRow:
    """One candidate training row: the features, the label, and the physics under it.

    ``day`` rides along so a run can report the span it actually trained on, and ``physical`` so the
    PV model can be fitted on the residual rather than the level.
    """

    day: date
    row: FeatureRow
    actual: float | None
    physical: float | None


def _role_for(model_key: str, training_data_source: str) -> str:
    """The toy model is the oracle on synthetic data, and an ordinary model on real data."""
    if model_key in {MODEL_PERSISTENCE, MODEL_SEASONAL_NAIVE}:
        return ROLE_BASELINE
    if model_key == MODEL_TOY_PHYSICAL and training_data_source == TELEMETRY_SOURCE_SYNTHETIC:
        return ROLE_ORACLE
    return ROLE_MODEL


def _target_value(observation: Observation, target: str) -> float | None:
    return observation.pv_production_kwh if target == TARGET_PV else observation.household_load_kwh


def _fold_days(days: Sequence[date], config: ForecastConfig) -> list[tuple[int, date]]:
    """``(fold index, test day)`` -- one test day per stride, once enough history exists.

    Expanding window: fold *n* trains on everything observable at its test day's issue instant, so
    the training set grows and is never sampled from the future.
    """
    return [
        (index, day)
        for index, day in enumerate(days)
        if index >= config.min_train_days
        and (index - config.min_train_days) % config.fold_stride_days == 0
    ]


def _hours_of_day(observations: Sequence[Observation], day: date) -> list[Observation]:
    return [observation for observation in observations if observation.local_date == day]


def backtest(
    site: Site,
    target: str,
    window_start: date,
    window_end: date,
    observations: Sequence[Observation],
    vintages: Sequence[VintageHour],
    *,
    config: ForecastConfig,
    pv: PvSystemConfig,
) -> BacktestResult:
    """Evaluate every model for one target over one declared window."""
    history: dict[int, float | None] = {
        observation.ts_utc_ms: _target_value(observation, target) for observation in observations
    }
    by_issue: dict[int, dict[int, Mapping[str, float | None]]] = {}
    for vintage in vintages:
        by_issue.setdefault(vintage.issue_ms, {})[vintage.target_ts_utc_ms] = vintage.values
    issue_times = sorted(by_issue)

    days = sorted({observation.local_date for observation in observations})

    # Build features for EVERY day up front, not only for the days that become test folds. The
    # expanding training window is then a property of the data -- "every row observable at this
    # fold's issue instant" -- rather than of how often folds happen to be taken. Deriving it from
    # loop order instead would silently shrink the training set to one row per fold as the stride
    # widened, which is the sort of coupling that shows up as an unexplained accuracy cliff.
    prepared: dict[date, _DayData] = {}
    skipped: list[date] = []
    feature_names: tuple[str, ...] = ()

    for day in days:
        issue_ms = issue_time_ms(day)
        vintage_ms = select_vintage(issue_times, day)
        if vintage_ms is None:
            # No forecast existed before this day began. No prediction rather than a fabricated one.
            skipped.append(day)
            continue
        day_hours = _hours_of_day(observations, day)
        rows, actuals = _build_rows(
            day_hours, by_issue[vintage_ms], issue_ms, vintage_ms, history, site, pv, config, target
        )
        if not rows:
            skipped.append(day)
            continue
        toy_kwh, pvlib_kwh = _physical_kwh(day_hours, by_issue[vintage_ms], site, pv, target)
        prepared[day] = _DayData(
            rows=tuple(rows),
            actuals=tuple(actuals),
            vintage_ms=vintage_ms,
            toy_kwh=toy_kwh,
            pvlib_kwh=pvlib_kwh,
        )
        feature_names = rows[0].names

    collected: dict[str, list[Prediction]] = {}
    every_row: list[_TrainRow] = [
        _TrainRow(day=day, row=row, actual=actual, physical=physical)
        for day in days
        if day in prepared
        for row, actual, physical in zip(
            prepared[day].rows,
            prepared[day].actuals,
            prepared[day].pvlib_kwh or (None,) * len(prepared[day].rows),
            strict=True,
        )
    ]
    training_rows = 0
    train_span: tuple[date | None, date | None] = (None, None)

    prepared_days = [day for day in days if day in prepared]
    folds = _fold_days(prepared_days, config)

    # A window with too few days to train on reports its baselines and nothing else -- the case
    # `assert_baselines_exist_for_every_model` documents as legitimate. Two boundaries meet here and
    # both matter:
    #
    # * Emitting *nothing* (the obvious reading of "no folds") makes the window vanish from
    #   `mart_forecast_eval` entirely, and that dbt test then passes vacuously over a window nobody
    #   can see is missing. The two DST weeks were absent from the eval mart on exactly this route.
    # * Emitting the *physical* models as well would be worse than absence. On a seven-day window
    #   with no prior history `seasonal_naive` can never resolve an equivalent hour, so it scores no
    #   hours and drops out of the mart -- leaving a model row with no baseline beside it, which is
    #   the one arrangement the mart exists to prevent.
    #
    # Fold 0 here means "no cross-validation was run", and `fitting` keeps the boosted models
    # genuinely unfitted rather than trusting `_fit_and_predict`'s row-count floor to enforce
    # `min_train_days` by accident.
    fitting = bool(folds)
    schedule = folds if fitting else [(0, day) for day in prepared_days]

    for fold_index, day in schedule:
        prepared_day = prepared[day]
        issue_ms = prepared_day.rows[0].issue_ms
        day_hours = _hours_of_day(observations, day)

        # Every row whose actual had been observed by this fold's issue instant. The lag does the
        # work here: it is what stops the fold immediately before this one from being training data.
        train = (
            [
                candidate
                for candidate in every_row
                if candidate.row.ts_utc_ms + config.telemetry_lag_hours * _HOUR_MS <= issue_ms
            ]
            if fitting
            else []
        )
        if len(train) > training_rows:
            training_rows = len(train)
            train_span = (min(t.day for t in train), max(t.day for t in train))

        for model_key, predictions in _predict_all(
            target=target,
            day_data=prepared_day,
            day_hours=day_hours,
            issue_ms=issue_ms,
            history=history,
            fold=fold_index,
            train=train,
            site=site,
            config=config,
            baselines_only=not fitting,
        ).items():
            collected.setdefault(model_key, []).extend(predictions)

    runs = tuple(
        _model_run(
            site=site,
            target=target,
            model_key=model_key,
            window_start=window_start,
            window_end=window_end,
            train_span=train_span,
            training_rows=training_rows,
            feature_names=feature_names,
            predictions=predictions,
            config=config,
        )
        for model_key, predictions in sorted(collected.items())
    )
    return BacktestResult(runs=runs, skipped_days=tuple(skipped))


def _model_run(
    *,
    site: Site,
    target: str,
    model_key: str,
    window_start: date,
    window_end: date,
    train_span: tuple[date | None, date | None],
    training_rows: int,
    feature_names: tuple[str, ...],
    predictions: Sequence[Prediction],
    config: ForecastConfig,
) -> ModelRun:
    """One run record. The training span is the span actually fitted on, or null for the rest.

    Reporting the *window* bounds here would be worse than uninformative: a ``train_end`` equal to
    the last evaluated day is precisely the lookahead this milestone exists to disprove, written
    into the provenance table by the code that avoided it.
    """
    trains = model_key not in TRAINING_FREE_MODELS
    train_start, train_end = train_span if trains else (None, None)
    return ModelRun(
        site=site.id,
        target=target,
        model_key=model_key,
        role=_role_for(model_key, config.training_data_source),
        window_start=window_start,
        window_end=window_end,
        train_start=train_start,
        train_end=train_end,
        config_hash=_run_card(
            site, target, model_key, train_start, train_end, feature_names, config
        ).config_hash,
        n_train_rows=training_rows if trains else 0,
        feature_names=feature_names,
        predictions=tuple(predictions),
    )


def _run_card(
    site: Site,
    target: str,
    model_key: str,
    train_start: date | None,
    train_end: date | None,
    feature_names: tuple[str, ...],
    config: ForecastConfig,
) -> ModelCard:
    return ModelCard(
        model_key=model_key,
        target=target,
        site=site.id,
        training_data_source=config.training_data_source,
        selection_rule_id=SELECTION_RULE_ID,
        telemetry_lag_hours=config.telemetry_lag_hours,
        train_start=train_start.isoformat() if train_start else "",
        train_end=train_end.isoformat() if train_end else "",
        feature_names=feature_names,
        hyperparameters=HYPERPARAMETERS,
        quantiles=QUANTILES,
        library_versions=library_versions(),
        seed=config.seed,
        min_train_days=config.min_train_days,
        fold_stride_days=config.fold_stride_days,
    )


def _physical_kwh(
    day_hours: Sequence[Observation],
    forecast: Mapping[int, Mapping[str, float | None]],
    site: Site,
    pv: PvSystemConfig,
    target: str,
) -> tuple[tuple[float | None, ...] | None, tuple[float | None, ...] | None]:
    """``(toy, pvlib)`` AC kWh per hour of the day, or ``(None, None)`` for a non-PV target.

    Computed for every prepared day rather than only for fold days, because the pvlib chain is both
    a reported model *and* the baseline ``pvlib_hgb`` fits its residual against -- and the residual
    needs it for every training row, not just the day being predicted.
    """
    if target != TARGET_PV or not day_hours:
        return None, None
    ghi, direct, diffuse, temp, wind = (
        np.array([_weather(forecast, o.ts_utc_ms, name) for o in day_hours], dtype=float)
        for name in (
            "ghi_w_m2",
            "direct_radiation_w_m2",
            "diffuse_radiation_w_m2",
            "temperature_c",
            "wind_speed_m_s",
        )
    )
    ts_ms = np.array([o.ts_utc_ms for o in day_hours], dtype=np.int64)
    geometry = physical.solar_geometry(ts_ms, site.latitude, site.longitude)
    toy = physical.toy_ac_kwh(ghi, temp, pv)
    poa = physical.pvlib_ac_kwh(ghi, direct, diffuse, temp, wind, geometry, pv)
    return (
        tuple(_none_if_nan(v) for v in toy),
        tuple(_none_if_nan(v) for v in poa),
    )


def _build_rows(
    day_hours: Sequence[Observation],
    forecast: Mapping[int, Mapping[str, float | None]],
    issue_ms: int,
    vintage_ms: int,
    history: Mapping[int, float | None],
    site: Site,
    pv: PvSystemConfig,
    config: ForecastConfig,
    target: str,
) -> tuple[list[FeatureRow], list[float | None]]:
    """One feature row per hour of the target day, plus that hour's actual."""
    if not day_hours:
        return [], []
    ts_ms = np.array([observation.ts_utc_ms for observation in day_hours], dtype=np.int64)
    geometry = physical.solar_geometry(ts_ms, site.latitude, site.longitude)

    rows: list[FeatureRow] = []
    actuals: list[float | None] = []
    for index, observation in enumerate(day_hours):
        # Keyed off the declared column tuple rather than off whatever the vintage happens to hold
        # for this hour. A vintage that stops short of the window's last day would otherwise yield
        # an empty mapping, a feature row six columns narrower than its neighbours, and a ValueError
        # out of `build_matrix` aborting the whole backtest. A gap is a gap: NaN, not a short row.
        weather = {
            name: forecast.get(observation.ts_utc_ms, {}).get(name) for name in VINTAGE_COLUMNS
        }
        lagged = baselines.persistence(
            observation.ts_utc_ms,
            history,
            issue_ms=issue_ms,
            lag_hours=config.telemetry_lag_hours,
        )
        geometry_features = tuple(
            Feature(name, FeatureKind.SOLAR_GEOMETRY, value)
            for name, value in physical.geometry_feature_values(geometry, index).items()
        )
        lag_feature = (
            lagged_actual_feature(f"{target}_lag", lagged[1], lagged[0], config.telemetry_lag_hours)
            if lagged is not None
            else Feature(f"{target}_lag", FeatureKind.LAGGED_ACTUAL, None, available_at_ms=issue_ms)
        )
        rows.append(
            FeatureRow(
                ts_utc_ms=observation.ts_utc_ms,
                issue_ms=issue_ms,
                horizon_hour=int((observation.ts_utc_ms - issue_ms) // _HOUR_MS),
                features=(
                    *calendar_features(observation.ts_utc_ms),
                    *geometry_features,
                    *vintage_features(weather, vintage_ms),
                    lag_feature,
                ),
            )
        )
        actuals.append(_target_value(observation, target))
    return rows, actuals


def _predict_all(
    *,
    target: str,
    day_data: _DayData,
    day_hours: Sequence[Observation],
    issue_ms: int,
    history: Mapping[int, float | None],
    fold: int,
    train: Sequence[_TrainRow],
    site: Site,
    config: ForecastConfig,
    baselines_only: bool = False,
) -> dict[str, list[Prediction]]:
    """Every model's prediction for one target day, all against the same information set."""
    out: dict[str, list[Prediction]] = {}
    rows = day_data.rows
    actuals = day_data.actuals
    vintage_ms = day_data.vintage_ms

    def emit(model_key: str, values: Sequence[float | None]) -> None:
        out[model_key] = [
            Prediction(
                ts_utc_ms=row.ts_utc_ms,
                local_date=observation.local_date,
                horizon_hour=row.horizon_hour,
                issue_ms=issue_ms,
                vintage_issue_ms=vintage_ms,
                fold=fold,
                p10_kwh=None,
                p50_kwh=value,
                p90_kwh=None,
                actual_kwh=actual,
            )
            for row, observation, value, actual in zip(
                rows, day_hours, values, actuals, strict=True
            )
        ]

    lag = config.telemetry_lag_hours
    emit(
        MODEL_PERSISTENCE,
        [
            found[1]
            if (
                found := baselines.persistence(
                    o.ts_utc_ms, history, issue_ms=issue_ms, lag_hours=lag
                )
            )
            else None
            for o in day_hours
        ],
    )
    emit(
        MODEL_SEASONAL_NAIVE,
        [
            found[1]
            if (
                found := baselines.seasonal_naive(
                    o.ts_utc_ms, history, issue_ms=issue_ms, lag_hours=lag
                )
            )
            else None
            for o in day_hours
        ],
    )

    if baselines_only:
        return out

    if target == TARGET_PV and day_data.toy_kwh is not None and day_data.pvlib_kwh is not None:
        emit(MODEL_TOY_PHYSICAL, day_data.toy_kwh)
        emit(MODEL_PVLIB_PHYSICAL, day_data.pvlib_kwh)
        hgb_key, physical_baseline = MODEL_PVLIB_HGB, day_data.pvlib_kwh
    else:
        hgb_key, physical_baseline = MODEL_LOAD_HGB, None

    quantiles = _fit_and_predict(rows, train, physical_baseline, config, site, target, hgb_key)
    if quantiles is not None:
        out[hgb_key] = [
            Prediction(
                ts_utc_ms=row.ts_utc_ms,
                local_date=observation.local_date,
                horizon_hour=row.horizon_hour,
                issue_ms=issue_ms,
                vintage_issue_ms=vintage_ms,
                fold=fold,
                p10_kwh=_none_if_nan(quantiles[index][0]),
                p50_kwh=_none_if_nan(quantiles[index][1]),
                p90_kwh=_none_if_nan(quantiles[index][2]),
                actual_kwh=actual,
            )
            for index, (row, observation, actual) in enumerate(
                zip(rows, day_hours, actuals, strict=True)
            )
        ]
    return out


def _fit_and_predict(
    rows: Sequence[FeatureRow],
    train: Sequence[_TrainRow],
    physical_baseline: Sequence[float | None] | None,
    config: ForecastConfig,
    site: Site,
    target: str,
    model_key: str,
) -> NDArray[np.float64] | None:
    """Fit on the observable history and predict this day, or ``None`` if there is nothing to fit.

    For PV the model learns the *residual* against the physical chain rather than the level, which
    is the classic hybrid: physics supplies the shape, boosting supplies the correction. On
    synthetic windows that correction is largely the plane-of-array transposition the truth model
    never had -- which is real signal about this simulator and no signal at all about a real roof.
    That is the whole basis of the provenance interlock in ``models.load_artifact``, so the residual
    has to be what is actually fitted: a level model wearing the label would make the interlock's
    refusal a superstition rather than a safeguard.

    A training row whose physical baseline is missing is dropped rather than fitted at its level --
    an undefined residual is not a zero one -- and a *predicted* hour with no baseline comes back
    ``NaN`` and is reported as no prediction.
    """
    labelled: list[tuple[_TrainRow, float]] = []
    for candidate in train:
        if candidate.actual is None:
            continue
        if physical_baseline is None:
            labelled.append((candidate, candidate.actual))
        elif candidate.physical is not None:
            labelled.append((candidate, candidate.actual - candidate.physical))
    if len(labelled) < len(QUANTILES) * 10:
        return None

    _, train_matrix = build_matrix([candidate.row for candidate, _ in labelled])
    train_target = np.array([label for _, label in labelled], dtype=np.float64)
    names, predict_matrix = build_matrix(list(rows))

    card = _run_card(
        site,
        target,
        model_key,
        min(candidate.day for candidate, _ in labelled),
        max(candidate.day for candidate, _ in labelled),
        names,
        config,
    )
    forecaster = fit_quantile_forecaster(train_matrix, train_target, card)
    predicted = forecaster.predict(predict_matrix)
    if physical_baseline is None:
        return predicted
    baseline = np.array(
        [np.nan if value is None else value for value in physical_baseline], dtype=np.float64
    )
    return predicted + baseline[:, None]


def _weather(forecast: Mapping[int, Mapping[str, float | None]], ts_ms: int, name: str) -> float:
    value = forecast.get(ts_ms, {}).get(name)
    return float("nan") if value is None else value


def _none_if_nan(value: float) -> float | None:
    return None if not np.isfinite(value) else float(value)


def coverage_days(window_start: date, window_end: date) -> list[date]:
    """Every Berlin calendar day in an inclusive window."""
    span = (window_end - window_start).days
    return [window_start + timedelta(days=offset) for offset in range(span + 1)]
