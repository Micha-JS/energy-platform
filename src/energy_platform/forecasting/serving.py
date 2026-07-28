"""Producing the forecasts a day-ahead plan is actually made from.

M7 backtests: it refits at every fold, predicts the fold's own test day, scores it, and throws the
fitted model away. That is the right shape for measuring accuracy and the wrong shape for using a
model, and the difference is not cosmetic -- a backtest answers *"how good would this have been?"*
while a household needs *"what does the model I already have say about tomorrow?"*

This module is the second shape, and it is the seam ``models.load_artifact`` was written for. Until
now nothing in the platform persisted a fitted model, so ``derived.forecast_runs.artifact_key`` was
null on every row and the provenance interlock guarded a path nobody walked. M8 walks it.

**The serving schedule mirrors the backtest's, deliberately.** Models are fitted on exactly M7's
fold cadence -- :func:`~energy_platform.forecasting.runner.fold_days`, so ``min_train_days`` of
warm-up and then a refit every ``fold_stride_days`` -- and each fitted model then serves every day
until the next refit. That is both realistic (nobody retrains nightly) and conservative: a day six
days after its fold is predicted by a model that has seen strictly less history than a backtest
fold would have given it. What it is *not* is a new no-lookahead surface. Every fit is on rows
``is_observable`` at its own fold's issue instant, every prediction resolves its vintage through
``select_vintage``, and every feature row passes ``check_no_lookahead`` on its way into the matrix.
The information set is M7's; only the cadence of use differs.

**A day the model has not warmed up for is not served at all.** The first ``min_train_days`` days of
a window have no fitted model, and a window shorter than that has none anywhere. Rather than
back-filling those days with a baseline forecast -- which would blend two very different forecast
qualities into one number and quietly flatter or damn it depending on the window -- they are
reported as unserved and the simulation that consumes this starts where the models do.

**The planner refuses the oracle.** On synthetic windows M3's flat-plate model *is* the process that
generated the PV labels, so planning with it would hand the day-ahead optimiser near-perfect
foresight and collapse the forecast-driven result onto the hindsight optimum. The captured-value
share would read close to 100% and would be measuring the simulator, not a forecast. So
:func:`planner_model_key` returns a model whose role is ``model`` and
:func:`assert_plannable_model` refuses anything else -- an interlock rather than a warning, for the
same reason ``load_artifact`` is one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from energy_platform.config import ForecastConfig, PvSystemConfig, Site
from energy_platform.forecasting import runner
from energy_platform.forecasting.models import (
    QuantileForecaster,
    load_artifact,
    save_artifact,
)
from energy_platform.forecasting.store import Observation, VintageHour
from energy_platform.forecasting.vintage import is_observable, issue_time_ms

# Which model plans each target. Both are the fitted ones -- the boosted models M7 scores as
# `model` -- and neither is a baseline or the oracle. Stated as a mapping rather than derived, so a
# change here is a visible change to what the headline figure measures.
PLANNER_MODELS: Final[Mapping[str, str]] = {
    runner.TARGET_PV: runner.MODEL_PVLIB_HGB,
    runner.TARGET_LOAD: runner.MODEL_LOAD_HGB,
}

# The index of the p50 column in `QuantileForecaster.predict`'s output, which returns the quantiles
# sorted per row. The plan is made on the median: a day-ahead schedule is a point decision, and
# planning on p10 or p90 would be a risk posture nobody chose.
_P50: Final = 1


class ServingError(RuntimeError):
    """Raised when a model may not be used to plan with, naming why."""


@dataclass(frozen=True, slots=True)
class HourForecast:
    """What the planner believes about one hour, and whether that belief was adjusted."""

    ts_utc_ms: int
    p50_kwh: float | None
    was_clamped: bool


@dataclass(frozen=True, slots=True)
class DayForecast:
    """One target's day-ahead forecast for one Berlin day, as of its decision time."""

    target: str
    target_day: date
    decision_ms: int
    vintage_issue_ms: int
    model_key: str
    config_hash: str
    fit_day: date
    hours: tuple[HourForecast, ...]

    @property
    def is_complete(self) -> bool:
        """Whether every hour resolved. An incomplete day is not planned; ``dispatch.forward``
        falls back to the naive policy for it rather than planning around a hole."""
        return bool(self.hours) and all(hour.p50_kwh is not None for hour in self.hours)

    @property
    def clamped_hours(self) -> int:
        return sum(1 for hour in self.hours if hour.was_clamped)

    def values(self) -> tuple[float | None, ...]:
        return tuple(hour.p50_kwh for hour in self.hours)


@dataclass(frozen=True, slots=True)
class ServingModel:
    """A fitted planner model, the fold it was fitted at, and where it was persisted."""

    target: str
    model_key: str
    fit_day: date
    fit_issue_ms: int
    train_start: date
    train_end: date
    n_train_rows: int
    config_hash: str
    artifact_dir: Path
    forecaster: QuantileForecaster


@dataclass(frozen=True, slots=True)
class ServingPlan:
    """Every day of one window this target could be forecast for, and the models that did it."""

    target: str
    model_key: str
    window_start: date
    window_end: date
    days: tuple[DayForecast, ...]
    models: tuple[ServingModel, ...]
    unserved_days: tuple[date, ...]

    @property
    def served_days(self) -> tuple[date, ...]:
        return tuple(day.target_day for day in self.days)

    @property
    def by_day(self) -> dict[date, DayForecast]:
        return {day.target_day: day for day in self.days}


def planner_model_key(target: str) -> str:
    """Which model plans ``target``, refusing a target nothing is fitted for."""
    try:
        return PLANNER_MODELS[target]
    except KeyError:
        raise ServingError(
            f"no planner model for target {target!r}; expected one of {sorted(PLANNER_MODELS)}"
        ) from None


def assert_plannable_model(model_key: str, config: ForecastConfig) -> None:
    """Refuse to plan with a baseline or with the oracle.

    The oracle case is the one that matters and it is silent if unguarded. On a synthetic window
    ``toy_physical`` is M3's own generator, so a plan made from its output would be a plan made with
    the answers -- forecast-driven dispatch would land on top of the hindsight optimum, regret would
    read as zero, and the headline would be a statement about the simulator wearing the clothes of a
    result. The role is already computed for the eval mart; this reuses it rather than re-deciding.
    """
    role = runner.role_for(model_key, config.training_data_source)
    if role != runner.ROLE_MODEL:
        raise ServingError(
            f"refusing to plan with {model_key!r}: its role is {role!r}, not "
            f"{runner.ROLE_MODEL!r}. On {config.training_data_source} data that model is not a "
            "forecast of the future -- planning with it would measure the data-generating process "
            "rather than forecast skill."
        )


def serve_window(
    site: Site,
    target: str,
    window_start: date,
    window_end: date,
    observations: Sequence[Observation],
    vintages: Sequence[VintageHour],
    *,
    config: ForecastConfig,
    pv: PvSystemConfig,
) -> ServingPlan:
    """Day-ahead forecasts for every servable day of one window.

    Same signature and the same purity as :func:`~energy_platform.forecasting.runner.backtest`: no
    connection is opened, so the whole of ``tests/dispatch`` can drive this without Postgres.

    The only side effect is on disk -- each fold's model is written to the gitignored artifact
    directory and read back through :func:`load_artifact`, so the provenance interlock sits on the
    production path rather than in a test. Reading back what was just written is not ceremony: it is
    what makes an artifact that cannot be reloaded fail here, at the fit, instead of on some later
    run that finds a directory it cannot use.
    """
    model_key = planner_model_key(target)
    assert_plannable_model(model_key, config)

    history: dict[int, float | None] = {
        observation.ts_utc_ms: runner.target_value(observation, target)
        for observation in observations
    }
    by_issue, issue_times = runner.index_vintages(vintages)
    days = sorted({observation.local_date for observation in observations})

    prepared, _ = runner.prepare_days(
        days,
        observations=observations,
        by_issue=by_issue,
        issue_times=issue_times,
        history=history,
        site=site,
        pv=pv,
        config=config,
        target=target,
    )
    prepared_days = [day for day in days if day in prepared]
    candidates = runner.training_rows(prepared, days)
    residual = target == runner.TARGET_PV

    models: list[ServingModel] = []
    for _, fit_day in runner.fold_days(prepared_days, config):
        fitted = _fit_at(
            fit_day,
            candidates=candidates,
            residual=residual,
            site=site,
            target=target,
            model_key=model_key,
            config=config,
        )
        if fitted is not None:
            models.append(fitted)

    served: list[DayForecast] = []
    unserved: list[date] = []
    for day in prepared_days:
        model = model_for_day(models, day)
        if model is None:
            # Inside the warm-up, or a window too short to have folds at all. Reported, not padded.
            unserved.append(day)
            continue
        served.append(_forecast_day(model, day, prepared[day], target=target, residual=residual))

    # A day with no vintage never reached `prepared` and is unserved for a different reason; both
    # are unserved from the consumer's point of view, and both must be visible to it.
    unserved.extend(day for day in days if day not in prepared)

    return ServingPlan(
        target=target,
        model_key=model_key,
        window_start=window_start,
        window_end=window_end,
        days=tuple(served),
        models=tuple(models),
        unserved_days=tuple(sorted(unserved)),
    )


def model_for_day(models: Sequence[ServingModel], day: date) -> ServingModel | None:
    """The most recently fitted model available on ``day``, or ``None`` before the first fit.

    At or before, never after: a model fitted on day F is available for F itself, because F's fit
    used only rows observable at F's own issue instant. Reaching forward to a later fold would be
    the plainest possible lookahead and is the thing this one comparison prevents.
    """
    eligible = [model for model in models if model.fit_day <= day]
    return max(eligible, key=lambda model: model.fit_day) if eligible else None


def _fit_at(
    fit_day: date,
    *,
    candidates: Sequence[runner.TrainRow],
    residual: bool,
    site: Site,
    target: str,
    model_key: str,
    config: ForecastConfig,
) -> ServingModel | None:
    """Fit one fold's model on what was observable at its issue instant, and persist it."""
    fit_issue_ms = issue_time_ms(fit_day)
    train = [
        candidate
        for candidate in candidates
        if is_observable(candidate.row.ts_utc_ms, fit_issue_ms, config.telemetry_lag_hours)
    ]
    fitted = runner.fit_model(
        train,
        residual=residual,
        config=config,
        site=site,
        target=target,
        model_key=model_key,
    )
    if fitted is None:
        return None
    forecaster, summary = fitted

    directory = save_artifact(forecaster, config)
    reloaded = load_artifact(directory, expected_data_source=config.training_data_source)
    return ServingModel(
        target=target,
        model_key=model_key,
        fit_day=fit_day,
        fit_issue_ms=fit_issue_ms,
        train_start=summary.start,
        train_end=summary.end,
        n_train_rows=summary.n_rows,
        config_hash=forecaster.card.config_hash,
        artifact_dir=directory,
        forecaster=reloaded,
    )


def _forecast_day(
    model: ServingModel,
    day: date,
    day_data: runner.DayData,
    *,
    target: str,
    residual: bool,
) -> DayForecast:
    """Predict one day from a fitted model.

    ``day_data.actuals`` is deliberately untouched. It exists because the same preparation feeds the
    backtest, which needs labels; a planner that read it would be reading the day it is predicting.
    Nothing here does, and ``build_matrix`` re-checks every feature's availability anyway -- the
    label is not a feature, so that check could not catch it, which is precisely why this sentence
    is here and why ``tests/dispatch/test_forward_lookahead.py`` asserts the plan is unchanged when
    the actuals are replaced with nonsense.
    """
    predicted = runner.predict_rows(
        model.forecaster,
        day_data.rows,
        day_data.pvlib_kwh if residual else None,
    )
    hours = tuple(
        _hour(row.ts_utc_ms, runner.none_if_nan(predicted[index][_P50]))
        for index, row in enumerate(day_data.rows)
    )
    return DayForecast(
        target=target,
        target_day=day,
        decision_ms=day_data.rows[0].issue_ms,
        vintage_issue_ms=day_data.vintage_ms,
        model_key=model.model_key,
        config_hash=model.config_hash,
        fit_day=model.fit_day,
        hours=hours,
    )


def _hour(ts_utc_ms: int, p50_kwh: float | None) -> HourForecast:
    """Clamp a negative forecast to zero, and say that it happened.

    Neither PV production nor household load can be negative, but a residual correction subtracted
    from a near-zero physical baseline can overshoot below it, and a boosted model has no notion of
    the sign it is supposed to respect. Feeding a negative kWh into the optimiser would not merely
    be wrong, it would be *exploitable*: negative load is a generator the MILP would happily sell
    from. Clamping is the honest repair, and counting the clamps keeps it from being a silent one --
    a day full of them is a broken forecast, not a cautious one.
    """
    if p50_kwh is None:
        return HourForecast(ts_utc_ms, None, was_clamped=False)
    if p50_kwh < 0.0:
        return HourForecast(ts_utc_ms, 0.0, was_clamped=True)
    return HourForecast(ts_utc_ms, p50_kwh, was_clamped=False)
