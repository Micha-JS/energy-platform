"""Naive baselines -- implemented first, and reported beside every model.

Beating a naive baseline is the credibility test for a forecast, so these are not an afterthought:
they are the row every model row is read against, and ``mart_forecast_eval`` refuses to hold a model
without them (``assert_baselines_exist_for_every_model.sql``).

Both are the textbook baselines with one correction that this platform forces. "Persistence" usually
means *yesterday's* value at the same hour -- but yesterday has not been observed yet when the
observation lag is five days, so the textbook version would be reading the future and would produce
a baseline nothing could honestly beat. Instead both walk back until they reach a value that was
genuinely available at the issue instant. The rule is named
:data:`~energy_platform.forecasting.vintage.PERSISTENCE_RULE_ID` and persisted per row.

The consequence is worth stating plainly, because a reader will otherwise assume an off-by-one: on
synthetic data persistence is *six or seven days* back, not one. On a real house, where telemetry
arrives live, the same code gives the textbook one-day baseline -- the rule is one rule and only the
lag differs, which is why it is configuration rather than a branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from energy_platform.forecasting.vintage import equivalent_hour_ms, is_observable

# Stride of each baseline, in local calendar days.
PERSISTENCE_STRIDE_DAYS: Final = 1
SEASONAL_NAIVE_STRIDE_DAYS: Final = 7

# How far back to look before giving up. Generous enough to cross the observation lag plus a run of
# gaps, bounded so a sparsely-covered site cannot silently reach back a season for a value.
_MAX_STEPS: Final = 60

MODEL_PERSISTENCE: Final = "persistence"
MODEL_SEASONAL_NAIVE: Final = "seasonal_naive"


def latest_observed_value(
    target_ts_utc_ms: int,
    history: Mapping[int, float | None],
    *,
    issue_ms: int,
    lag_hours: int,
    stride_days: int,
    max_steps: int = _MAX_STEPS,
) -> tuple[int, float] | None:
    """The most recent equivalent hour that was both *observable* and actually *present*.

    Two distinct reasons to keep walking, and they are not the same thing: an hour may be too recent
    to have been observed at the issue instant, or it may have been observed and be missing anyway
    (a genuine gap -- the seeded June window has one, deliberately). Neither is a value, so both
    continue the walk; running out of steps returns ``None`` rather than the nearest thing to hand.

    Returns the instant alongside the value so the caller can declare the feature's availability
    from the hour it actually came from, not from an assumed offset.
    """
    for step in range(1, max_steps + 1):
        candidate = equivalent_hour_ms(target_ts_utc_ms, step * stride_days)
        if not is_observable(candidate, issue_ms, lag_hours):
            continue
        value = history.get(candidate)
        if value is not None:
            return candidate, value
    return None


def persistence(
    target_ts_utc_ms: int,
    history: Mapping[int, float | None],
    *,
    issue_ms: int,
    lag_hours: int,
) -> tuple[int, float] | None:
    """Same local hour, most recent *observable* day."""
    return latest_observed_value(
        target_ts_utc_ms,
        history,
        issue_ms=issue_ms,
        lag_hours=lag_hours,
        stride_days=PERSISTENCE_STRIDE_DAYS,
    )


def seasonal_naive(
    target_ts_utc_ms: int,
    history: Mapping[int, float | None],
    *,
    issue_ms: int,
    lag_hours: int,
) -> tuple[int, float] | None:
    """Same local hour, most recent observable *same weekday*.

    The stronger baseline for household load, which is strongly weekday-shaped -- M3's generator
    builds it from separate weekday and weekend hour-of-day profiles, so a same-weekday lookup is
    very close to reading the generator's own shape. On synthetic data that makes this the number to
    beat, and matching it is the expected outcome rather than a disappointing one.
    """
    return latest_observed_value(
        target_ts_utc_ms,
        history,
        issue_ms=issue_ms,
        lag_hours=lag_hours,
        stride_days=SEASONAL_NAIVE_STRIDE_DAYS,
    )
