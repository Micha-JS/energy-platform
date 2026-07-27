"""The harness must reject a leaked feature. This is the milestone's load-bearing test.

A backtesting harness that *cannot* catch a leaked feature is decorative: every number it produces
is unfalsifiable, because the one failure mode that would invalidate them all is the one it does not
look for. So rather than asserting that the honest feature set scores well, these tests deliberately
attempt each way of reading the future and assert the attempt is refused.

Four attempts, one per way the platform could leak:

1. a feature reading the target hour's own actual;
2. a vintage issued after the prediction, handed straight to the checker to bypass selection;
3. a persistence lag shorter than the observation lag -- the subtle one, and the one this platform
   is actually exposed to;
4. a feature that declines to declare when it became knowable at all.

And a **positive control**, which is not optional. A checker implemented as ``raise
LookaheadError`` unconditionally would pass all four attempts above. The honest feature set must
build cleanly, or the suite is asserting nothing.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from energy_platform.config import DEFAULT_TELEMETRY_LAG_HOURS
from energy_platform.forecasting.baselines import (
    PERSISTENCE_STRIDE_DAYS,
    SEASONAL_NAIVE_STRIDE_DAYS,
)
from energy_platform.forecasting.features import (
    Feature,
    FeatureKind,
    FeatureRow,
    build_matrix,
    calendar_features,
    check_no_lookahead,
    lagged_actual_feature,
    vintage_features,
)
from energy_platform.forecasting.vintage import (
    MAX_LOOKBACK_DAYS,
    equivalent_hour_ms,
    is_observable,
    issue_time_ms,
    latest_observable_equivalent,
    max_steps,
    observable_at,
    select_vintage,
)

TARGET_DAY = date(2024, 6, 12)
LAG_HOURS = DEFAULT_TELEMETRY_LAG_HOURS  # 120 h -- ERA5's settling delay, inherited by synthetic
_HOUR_MS = 3_600_000


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


ISSUE_MS = issue_time_ms(TARGET_DAY)
NOON_MS = _ms(datetime(2024, 6, 12, 12, 0, tzinfo=UTC))


def _honest_row(ts_utc_ms: int = NOON_MS) -> FeatureRow:
    """The feature set the production models actually use, for one hour."""
    vintage_issue = _ms(
        datetime(2024, 6, 11, 16, 0, tzinfo=UTC)
    )  # 18:00 Berlin, the evening before
    observed = latest_observable_equivalent(
        ts_utc_ms, issue_ms=ISSUE_MS, lag_hours=LAG_HOURS, stride_days=1
    )
    assert observed is not None
    return FeatureRow(
        ts_utc_ms=ts_utc_ms,
        issue_ms=ISSUE_MS,
        horizon_hour=int((ts_utc_ms - ISSUE_MS) // _HOUR_MS),
        features=(
            *calendar_features(ts_utc_ms),
            *vintage_features({"ghi_w_m2": 610.0, "temperature_c": 21.0}, vintage_issue),
            lagged_actual_feature("pv_lag_persistence", 4.2, observed, LAG_HOURS),
        ),
    )


# -- Attempt 1: read the target hour's own actual ----------------------------------------


def test_a_feature_reading_the_target_hours_own_actual_is_rejected() -> None:
    """The crudest leak, and the one every backtest bug eventually reduces to."""
    row = FeatureRow(
        ts_utc_ms=NOON_MS,
        issue_ms=ISSUE_MS,
        horizon_hour=12,
        features=(
            *calendar_features(NOON_MS),
            lagged_actual_feature("pv_actual_now", 5.9, NOON_MS, LAG_HOURS),
        ),
    )
    with pytest.raises(Exception, match="pv_actual_now") as excinfo:
        check_no_lookahead(row)
    assert "before it existed" in str(excinfo.value)


# -- Attempt 2: a vintage issued after the prediction -------------------------------------


def test_a_vintage_issued_after_the_prediction_is_rejected() -> None:
    """Bypasses select_vintage on purpose -- 'guaranteed by construction' is what this catches.

    The selection rule already makes this unreachable through the production path. The check exists
    for the second call site nobody has written yet.
    """
    same_day_morning = _ms(datetime(2024, 6, 12, 6, 0, tzinfo=UTC))
    assert same_day_morning > ISSUE_MS
    row = FeatureRow(
        ts_utc_ms=NOON_MS,
        issue_ms=ISSUE_MS,
        horizon_hour=12,
        features=vintage_features({"ghi_w_m2": 610.0}, same_day_morning),
    )
    with pytest.raises(Exception, match="fc_ghi_w_m2"):
        check_no_lookahead(row)


def test_select_vintage_excludes_one_issued_at_the_target_day_boundary() -> None:
    """Strictly-before, and the recorded offline fixture sits exactly on that boundary."""
    midnight = issue_time_ms(TARGET_DAY)
    evening_before = _ms(datetime(2024, 6, 11, 16, 0, tzinfo=UTC))
    assert select_vintage([evening_before, midnight], TARGET_DAY) == evening_before


def test_select_vintage_returns_none_when_nothing_qualifies() -> None:
    """No fabricated fallback to the nearest vintage: a day with no forecast gets no prediction."""
    after = _ms(datetime(2024, 6, 12, 6, 0, tzinfo=UTC))
    assert select_vintage([after], TARGET_DAY) is None


def test_select_vintage_takes_the_latest_of_several_eligible() -> None:
    early = _ms(datetime(2024, 6, 9, 16, 0, tzinfo=UTC))
    late = _ms(datetime(2024, 6, 11, 16, 0, tzinfo=UTC))
    assert select_vintage([early, late], TARGET_DAY) == late


# -- Attempt 3: a persistence lag shorter than the observation lag -------------------------


def test_yesterdays_value_is_lookahead_on_this_platform() -> None:
    """The subtle one. 'Persistence = yesterday' is wrong here and would beat every model.

    Synthetic telemetry is generated from ERA5-backed archive weather that settles ~5 days late, so
    yesterday's PV has not been observed when today's prediction is issued. A harness that let this
    through would report a persistence baseline nothing could beat, and the honest conclusion --
    that the models are fine and the baseline is cheating -- is not one anybody reaches by looking
    at a leaderboard.
    """
    yesterday_noon = equivalent_hour_ms(NOON_MS, days_back=1)
    assert not is_observable(yesterday_noon, ISSUE_MS, LAG_HOURS)

    row = FeatureRow(
        ts_utc_ms=NOON_MS,
        issue_ms=ISSUE_MS,
        horizon_hour=12,
        features=(lagged_actual_feature("pv_lag_1d", 4.2, yesterday_noon, LAG_HOURS),),
    )
    with pytest.raises(Exception, match="pv_lag_1d"):
        check_no_lookahead(row)


def test_the_baseline_walks_back_until_the_value_was_actually_observed() -> None:
    """...and the honest baseline it lands on is accepted."""
    observed = latest_observable_equivalent(
        NOON_MS, issue_ms=ISSUE_MS, lag_hours=LAG_HOURS, stride_days=1
    )
    assert observed is not None
    assert is_observable(observed, ISSUE_MS, LAG_HOURS)
    assert observable_at(observed, LAG_HOURS) <= ISSUE_MS
    # Six days back, not one: 120 h of lag plus the noon-to-midnight offset.
    assert (NOON_MS - observed) // (24 * _HOUR_MS) == 6


def test_the_hour_beginning_at_the_issue_instant_is_not_observable_at_it() -> None:
    """The zero-lag leak, at the one instant where only the interval term stands in the way.

    An hourly reading stamped at ``ts`` summarises ``[ts, ts + 1h)``, so the hour that *begins* when
    the prediction is issued has not finished happening. Without that term a lag-0 platform -- a
    real house on Home Assistant, and every fixture in ``test_backtest`` -- trains each fold on the
    first hour of the very day it is scoring, and no feature-level check can see it: the leaked
    quantity is the label, not a feature.
    """
    assert not is_observable(ISSUE_MS, ISSUE_MS, 0)
    assert is_observable(ISSUE_MS - _HOUR_MS, ISSUE_MS, 0)
    assert observable_at(ISSUE_MS, 0) == ISSUE_MS + _HOUR_MS


def test_a_zero_lag_platform_gets_a_genuine_yesterday_baseline() -> None:
    """A real house reports live, so the same code yields the textbook baseline. The rule is one
    rule; only the lag differs, which is why it is configuration and not a branch."""
    observed = latest_observable_equivalent(NOON_MS, issue_ms=ISSUE_MS, lag_hours=0, stride_days=1)
    assert observed == equivalent_hour_ms(NOON_MS, days_back=1)


def test_no_observable_equivalent_yields_none_rather_than_reaching_further_back() -> None:
    """Three daily steps cannot clear a five-day lag, so there is no honest answer to give.

    Returning ``None`` rather than the nearest available value matters: a baseline that quietly
    reached past its own search bound would be a different baseline than the one the mart names.
    """
    observed = latest_observable_equivalent(
        NOON_MS, issue_ms=ISSUE_MS, lag_hours=LAG_HOURS, stride_days=1, steps=3
    )
    assert observed is None


def test_the_lookback_bound_is_the_same_distance_at_every_stride() -> None:
    """Bounding steps rather than days let the weekly baseline reach back fourteen months.

    ``seasonal_naive`` strides by seven, so a sixty-*step* bound was ~420 days -- and a value from
    over a year earlier was reported in the mart under the same ``persistence_rule_id`` as a
    one-week lookup. The bound has to be stated in the unit the claim is made in.
    """
    for stride in (PERSISTENCE_STRIDE_DAYS, SEASONAL_NAIVE_STRIDE_DAYS):
        assert max_steps(stride) * stride <= MAX_LOOKBACK_DAYS
    assert max_steps(SEASONAL_NAIVE_STRIDE_DAYS) * SEASONAL_NAIVE_STRIDE_DAYS >= LAG_HOURS // 24


# -- Attempt 4: decline to declare availability -------------------------------------------


@pytest.mark.parametrize("kind", [FeatureKind.VINTAGE, FeatureKind.LAGGED_ACTUAL])
def test_a_data_derived_feature_must_declare_when_it_became_knowable(kind: FeatureKind) -> None:
    """Omitting availability would make a feature silently exempt from every check above."""
    with pytest.raises(ValueError, match="must declare available_at_ms"):
        Feature("smuggled", kind, 1.0)


@pytest.mark.parametrize("kind", [FeatureKind.CALENDAR, FeatureKind.SOLAR_GEOMETRY])
def test_calendar_and_geometry_are_the_only_features_knowable_ahead(kind: FeatureKind) -> None:
    """Which hour it is, and where the sun will be, do not wait for data."""
    assert Feature("fine", kind, 1.0).available_at_ms is None


# -- The positive control, without which none of the above means anything -----------------


def test_the_honest_feature_set_builds_cleanly() -> None:
    """A checker that rejected everything would pass every rejection test above.

    This is the same empty-input sentinel every singular test in dbt/tests/ carries, for the same
    reason: a guard that never fires and a guard that always fires are indistinguishable from their
    pass/fail record alone.
    """
    rows = [_honest_row(NOON_MS + hour * _HOUR_MS) for hour in range(-12, 12)]
    names, matrix = build_matrix(rows)
    assert matrix.shape == (len(rows), len(names))
    assert "local_hour" in names
    assert "fc_ghi_w_m2" in names
    assert "pv_lag_persistence" in names


def test_a_missing_value_survives_as_nan_rather_than_being_imputed() -> None:
    """NaN over fabrication, all the way into the model -- HGB routes it down a learned branch."""
    row = FeatureRow(
        ts_utc_ms=NOON_MS,
        issue_ms=ISSUE_MS,
        horizon_hour=12,
        features=vintage_features(
            {"ghi_w_m2": None}, _ms(datetime(2024, 6, 11, 16, 0, tzinfo=UTC))
        ),
    )
    _, matrix = build_matrix([row])
    assert matrix.shape == (1, 1)
    assert bool(matrix[0][0] != matrix[0][0])  # NaN is the only value unequal to itself


def test_rows_with_disagreeing_feature_order_are_refused() -> None:
    """A matrix whose columns mean different things per row produces plausible numbers forever."""
    vintage_issue = _ms(datetime(2024, 6, 11, 16, 0, tzinfo=UTC))
    a = FeatureRow(NOON_MS, ISSUE_MS, 12, vintage_features({"a": 1.0, "b": 2.0}, vintage_issue))
    b = FeatureRow(NOON_MS, ISSUE_MS, 12, vintage_features({"a": 1.0, "c": 3.0}, vintage_issue))
    with pytest.raises(ValueError, match="feature names differ"):
        build_matrix([a, b])


def test_an_empty_matrix_is_empty_rather_than_an_error() -> None:
    names, matrix = build_matrix([])
    assert names == ()
    assert matrix.size == 0
