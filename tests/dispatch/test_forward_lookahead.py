"""The no-lookahead guard, extended from forecasting to dispatch. The milestone's load-bearing test.

M7 established that a *prediction* may only use what existed when it was issued. M8 makes
decisions, and a decision has a second way to cheat that a prediction does not: it can read a
**price** that had not been published yet. The day-ahead auction for delivery day D clears around
12:45 the previous lunchtime, so at D 00:00 Berlin the whole of D's prices are settled fact -- but
D+1's are not, and a planner that quietly reached for them would look, from the outside, exactly
like a very good planner.

So this file makes deliberate attempts to cheat, in the shape
``tests/forecasting/test_lookahead_rejection.py`` established, and asserts each is refused:

1. a weather vintage issued after the decision time, handed straight to the checker;
2. a day whose day-ahead prices had not been published at the decision time;
3. the target day's own actuals reaching the plan through the preparation object that carries them.

And **positive controls**, which are not optional: a checker that rejected everything would pass
every rejection test above, and a planner that produced no plan would pass every leak test ever
written.

Nothing here touches Postgres or the network, and nothing fits a model -- the serving plans are
built by hand (see ``conftest.serving_plan``), so the simulation is exercised without a training
loop in the way.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from energy_platform.dispatch import decision, forward
from energy_platform.dispatch.decision import (
    DecisionTimeError,
    assert_prices_published,
    decision_time_ms,
    price_publication_ms,
    prices_are_published,
)
from energy_platform.dispatch.model import Scenario
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.forecasting.features import (
    FeatureRow,
    LookaheadError,
    build_matrix,
    check_no_lookahead,
    vintage_features,
)
from energy_platform.forecasting.serving import ServingError, assert_plannable_model
from energy_platform.forecasting.vintage import issue_time_ms
from tests.dispatch.conftest import (
    BARE_DYNAMIC,
    FEED_IN,
    LOSSLESS,
    berlin_days,
    serving_plan,
    simulated_hours,
)

TARGET_DAY = date(2024, 5, 15)
_HOUR_MS = 3_600_000


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# -- The decision time is M7's issue instant, not a second opinion about it -----------------


@pytest.mark.parametrize(
    "day",
    [
        date(2024, 3, 31),  # spring forward: a 23-hour Berlin day
        date(2024, 5, 15),  # an ordinary summer day
        date(2024, 10, 27),  # fall back: a 25-hour Berlin day
        date(2024, 12, 31),  # a year boundary, in winter time
    ],
)
def test_the_decision_time_is_exactly_m7s_issue_instant(day: date) -> None:
    """Two modules name the same instant, and neither may drift.

    M7 chose D 00:00 Europe/Berlin *for this consumer* and said so in ``vintage.py``. If the two
    ever disagreed, every stored prediction would have been issued under one rule and consumed under
    another, and the mismatch would be invisible -- the forecasts would simply be slightly wrong in
    a direction nobody could name. Both resolve through ``berlin_day_window``, so this test is
    checking that neither has grown its own arithmetic.
    """
    assert decision_time_ms(day) == issue_time_ms(day)


# -- Attempt 1: a vintage issued after the decision time ------------------------------------


def test_a_vintage_issued_after_the_decision_time_is_rejected() -> None:
    """The planner's information set is the forecast's, so M7's checker guards it unchanged.

    Bypasses ``select_vintage`` on purpose. The selection rule already makes this unreachable
    through the production path; the check exists for the second call site, and M8 *is* that second
    call site -- the first consumer of a fitted model outside the backtest that scored it.
    """
    decision_ms = decision_time_ms(TARGET_DAY)
    same_day_morning = _ms(datetime(2024, 5, 15, 6, 0, tzinfo=UTC))
    assert same_day_morning > decision_ms

    row = FeatureRow(
        ts_utc_ms=decision_ms + 12 * _HOUR_MS,
        issue_ms=decision_ms,
        horizon_hour=12,
        features=vintage_features({"ghi_w_m2": 610.0}, same_day_morning),
    )
    with pytest.raises(LookaheadError, match="fc_ghi_w_m2"):
        check_no_lookahead(row)


def test_the_serving_path_checks_every_row_it_predicts_from() -> None:
    """The guard has to be on the path, not merely available to it.

    ``predict_rows`` goes through ``build_matrix``, which runs ``check_no_lookahead`` per row. This
    asserts that arrangement rather than trusting it: a serving path that assembled its own matrix
    would be exempt from every check above while looking identical from the outside.
    """
    decision_ms = decision_time_ms(TARGET_DAY)
    leaked = FeatureRow(
        ts_utc_ms=decision_ms,
        issue_ms=decision_ms,
        horizon_hour=0,
        features=vintage_features({"ghi_w_m2": 1.0}, decision_ms + _HOUR_MS),
    )
    with pytest.raises(LookaheadError):
        build_matrix([leaked])


# -- Attempt 2: prices that had not been published at the decision time ---------------------


def test_planned_day_prices_are_published_before_the_decision() -> None:
    """The positive control for the price rule: D's own auction cleared the previous lunchtime."""
    decision_ms = decision_time_ms(TARGET_DAY)
    assert price_publication_ms(TARGET_DAY) < decision_ms
    assert prices_are_published(TARGET_DAY, decision_ms)
    assert_prices_published(TARGET_DAY, decision_ms)  # must not raise


def test_the_next_days_prices_are_not_published_at_this_decision_time() -> None:
    """The cheat: extending a plan past the day whose auction has cleared.

    A two-day planning horizon built from one decision instant is the natural way to write this bug
    -- it looks like nothing more than a longer window -- and it reads half a day into the future.
    """
    decision_ms = decision_time_ms(TARGET_DAY)
    tomorrow = TARGET_DAY + timedelta(days=1)
    assert price_publication_ms(tomorrow) > decision_ms
    assert not prices_are_published(tomorrow, decision_ms)
    with pytest.raises(DecisionTimeError, match="unpublished auction"):
        assert_prices_published(tomorrow, decision_ms)


def test_the_publication_instant_is_local_and_follows_the_dst_switch() -> None:
    """Stated in Berlin wall-clock because the exchange's timetable is, so it moves with DST.

    Pinned in absolute time on both sides of the October switch: 12:45 local is 10:45 UTC in summer
    and 11:45 UTC in winter. A rule written in UTC would be 45 minutes wrong for half the year, in a
    direction that only ever makes the guard *weaker*.
    """
    summer = price_publication_ms(date(2024, 10, 27))  # published 26 Oct, still CEST
    winter = price_publication_ms(date(2024, 11, 5))  # published 4 Nov, CET
    assert datetime.fromtimestamp(summer / 1000, UTC).hour == 10
    assert datetime.fromtimestamp(winter / 1000, UTC).hour == 11
    assert datetime.fromtimestamp(summer / 1000, UTC).minute == 45


def test_the_simulation_refuses_a_day_whose_prices_had_not_cleared() -> None:
    """End to end: the guard is wired into the rolling loop, not just importable beside it.

    Forced by moving the publication rule *later* than the decision time, the only way to reach the
    refusal without asking the simulation to plan a day it has no data for. A planner that called
    the rule but ignored the result would pass every test above and fail this one.
    """
    days = berlin_days(3)
    hours = simulated_hours(days)
    window = CoverageWindow(days[0], days[-1])

    with pytest.MonkeyPatch.context() as patch:
        # Patched on `decision`, where `assert_prices_published` resolves it, rather than on
        # `forward` -- which imports the assertion, not the rule. 12:45 on the delivery day itself:
        # half a day after the decision was taken.
        patch.setattr(
            decision, "price_publication_ms", lambda day: decision_time_ms(day) + _HOUR_MS
        )
        with pytest.raises(DecisionTimeError):
            forward.simulate(
                window,
                "home",
                hours,
                BARE_DYNAMIC,
                FEED_IN,
                LOSSLESS,
                pv_plan=serving_plan("pv_production_kwh", hours, days),
                load_plan=serving_plan("household_load_kwh", hours, days),
            )


# -- Attempt 3: the target day's own actuals reaching the plan ------------------------------


def test_the_plan_does_not_change_when_the_target_days_actuals_are_replaced() -> None:
    """The leak no feature check can catch, because the leaked quantity is not a feature.

    ``runner.DayData`` carries the day's ``actuals`` -- it has to, the backtest needs labels -- and
    the serving path is handed the same object. Nothing in ``check_no_lookahead`` could see a
    planner reading them, because a label is not a feature and carries no availability stamp. So the
    property is asserted behaviourally instead: corrupt the actuals, leave the forecasts alone, and
    the plan must come out bit-identical.

    The *executed* result is expected to differ -- it is executed against those actuals, which is
    the whole point. It is the planned trajectory that must be blind to them.
    """
    days = berlin_days(3)
    hours = simulated_hours(days)
    window = CoverageWindow(days[0], days[-1])
    pv_plan = serving_plan("pv_production_kwh", hours, days, bias=0.8)
    load_plan = serving_plan("household_load_kwh", hours, days, bias=0.8)

    honest = forward.simulate(
        window,
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        LOSSLESS,
        pv_plan=pv_plan,
        load_plan=load_plan,
    )
    assert isinstance(honest, forward.ForwardSolution)

    # Same prices, same forecasts, wildly different physics. A planner peeking at the actuals would
    # produce a different schedule; one that cannot see them produces the same one.
    corrupted = tuple(
        type(hour)(
            ts_utc=hour.ts_utc,
            pv_production_kwh=(hour.pv_production_kwh or 0.0) * 5 + 3.0,
            household_load_kwh=(hour.household_load_kwh or 0.0) * 5 + 3.0,
            price_eur_mwh=hour.price_eur_mwh,
        )
        for hour in hours
    )
    peeked = forward.simulate(
        window,
        "home",
        corrupted,
        BARE_DYNAMIC,
        FEED_IN,
        LOSSLESS,
        pv_plan=pv_plan,
        load_plan=load_plan,
    )
    assert isinstance(peeked, forward.ForwardSolution)

    honest_plan = [
        (hour.planned_charge_kwh, hour.planned_discharge_kwh) for hour in honest.execution.hours
    ]
    peeked_plan = [
        (hour.planned_charge_kwh, hour.planned_discharge_kwh) for hour in peeked.execution.hours
    ]
    assert honest_plan == peeked_plan, (
        "the planned trajectory changed when only the actuals changed -- the planner is "
        "reading the day it is supposed to be predicting"
    )


# -- The oracle interlock: a model that IS the data-generating process may not plan ----------


def test_the_oracle_may_not_be_used_to_plan_with() -> None:
    """On synthetic data M3's flat-plate model is the generator, so planning with it is cheating.

    Not a lookahead in the vintage sense -- the toy model uses only forecast weather -- but the same
    failure in effect: it reproduces the labels, so forecast-driven dispatch would land on the
    hindsight optimum and the captured-value share would read as a triumph while measuring the
    simulator. A refusal rather than a note, for the reason ``load_artifact`` is one.
    """
    from energy_platform.config import ForecastConfig
    from energy_platform.forecasting.runner import MODEL_PERSISTENCE, MODEL_TOY_PHYSICAL

    synthetic = ForecastConfig()
    assert synthetic.training_data_source == "synthetic"
    with pytest.raises(ServingError, match="oracle"):
        assert_plannable_model(MODEL_TOY_PHYSICAL, synthetic)
    with pytest.raises(ServingError, match="baseline"):
        assert_plannable_model(MODEL_PERSISTENCE, synthetic)


def test_the_models_that_do_plan_are_accepted() -> None:
    """The positive control. A guard that refused everything would pass both refusals above."""
    from energy_platform.config import ForecastConfig
    from energy_platform.forecasting.serving import PLANNER_MODELS

    config = ForecastConfig()
    for model_key in PLANNER_MODELS.values():
        assert_plannable_model(model_key, config)  # must not raise


# -- The positive control for the whole file ------------------------------------------------


def test_an_honest_simulation_produces_a_plan_and_three_costed_scenarios() -> None:
    """Without this, every refusal above could be satisfied by a simulation that never runs."""
    days = berlin_days(4)
    hours = simulated_hours(days)
    outcome = forward.simulate(
        CoverageWindow(days[0], days[-1]),
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        LOSSLESS,
        pv_plan=serving_plan("pv_production_kwh", hours, days),
        load_plan=serving_plan("household_load_kwh", hours, days),
    )
    assert isinstance(outcome, forward.ForwardSolution)
    assert outcome.simulated_days == 4
    assert outcome.fallback_days == 0
    for scenario in (
        Scenario.NAIVE_CONTINUOUS,
        Scenario.FORECAST_DRIVEN,
        Scenario.PERFECT_FORESIGHT_PLAN,
        Scenario.OPTIMAL,
    ):
        assert outcome.result(scenario).priced_hours == len(hours)
    # Something was actually decided: a plan of all zeros would satisfy every assertion above.
    assert any(hour.planned_charge_kwh > 0 for hour in outcome.execution.hours)
    assert any(hour.planned_discharge_kwh > 0 for hour in outcome.execution.hours)
