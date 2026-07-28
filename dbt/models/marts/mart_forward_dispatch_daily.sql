-- The rolling simulation day by day: three scenarios, one row each per simulated Berlin day.
-- Grain: (window_start, window_end, region, tariff_id, scenario, local_date).
--
-- mart_dispatch_regret answers "how much did forecasting capture over the whole span". This answers
-- "where did it capture it, and where did it lose" -- which is the question a chart asks, and the
-- reason the cumulative arithmetic lives here rather than in the plotting script. A figure that
-- computes its own running totals is a second implementation of the comparison, and the first thing
-- to drift when a scenario is added.
--
-- WHY THE REFERENCE SCENARIOS HAVE DAILY ROWS AT ALL. Neither naive_continuous nor optimal was
-- *decided* day by day -- both are solved once over the whole span -- but both are settled hour by
-- hour, so their daily costs are a well-defined slice of that settlement rather than a re-solve.
-- What is not well defined for them is the plan, and the columns say so instead of guessing:
-- plan_status is 'not_planned' and the SoC and fit-day columns are null. Only forecast_driven has a
-- decision time, a plan it may have departed from, and a state of charge chained from the previous
-- day's *executed* trajectory.
--
-- CUMULATIVE COST IS A WINDOW FUNCTION OVER PRICED DAYS ONLY. A day the tariff could not price
-- contributes null, not zero, and is skipped by the running sum rather than flattening it -- the
-- same "NaN over fabrication" rule int_hourly_tariff_cost sets and every mart downstream keeps.

with days as (
    select
        d.window_start,
        d.window_end,
        d.region,
        d.tariff_id,
        d.scenario,
        d.local_date,
        d.decision_time,
        d.plan_status,
        d.soc_start_kwh,
        d.soc_end_kwh,
        d.clipped_hours,
        d.clamped_forecast_hours,
        d.pv_fit_day,
        d.load_fit_day,
        d.energy_cost_eur,
        d.feed_in_revenue_eur,
        d.net_cost_eur,
        d.priced_hours,
        r.sim_start,
        r.sim_end,
        r.is_simulated
    from {{ source('derived', 'forward_dispatch_days') }} d
    join {{ source('derived', 'forward_dispatch_runs') }} r
        on r.region = d.region
        and r.window_start = d.window_start
        and r.window_end = d.window_end
        and r.tariff_id = d.tariff_id
        and r.scenario = d.scenario
)

select
    window_start,
    window_end,
    region,
    tariff_id,
    scenario,
    local_date,
    sim_start,
    sim_end,

    decision_time,
    plan_status,
    -- How far into the simulation this day is. Makes the three scenarios line up on one x-axis
    -- without the reader having to do date arithmetic, and is DST-proof in a way an hour offset
    -- would not be.
    (local_date - sim_start) + 1                  as sim_day,

    soc_start_kwh,
    soc_end_kwh,
    clipped_hours,
    clamped_forecast_hours,
    pv_fit_day,
    load_fit_day,
    -- How stale the model that planned this day was. Zero on a refit day, up to fold_stride_days-1
    -- the day before the next one -- the cost of not retraining nightly, made visible so it can be
    -- checked against the regret rather than assumed to be negligible.
    case when pv_fit_day is not null then local_date - pv_fit_day end as pv_model_age_days,

    energy_cost_eur,
    feed_in_revenue_eur,
    net_cost_eur,

    sum(net_cost_eur) over (
        partition by window_start, window_end, region, tariff_id, scenario
        order by local_date
        rows between unbounded preceding and current row
    )                                             as cumulative_net_cost_eur,

    priced_hours,
    -- A whole day is 24 hours except on the two DST Sundays, and the calendar is the authority on
    -- which -- never a count of the rows being tested.
    {{ berlin_span_hours('local_date', 'local_date') }} as expected_hours,
    priced_hours < {{ berlin_span_hours('local_date', 'local_date') }} as is_partial_day,
    is_simulated

from days
