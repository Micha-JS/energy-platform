-- Forecast accuracy, per model per horizon hour, with baselines and the oracle beside every model.
--
-- Reads the Python-written `derived` tables the backtester produces (energy-platform forecast), so
-- it sits between two dbt invocations exactly as mart_dispatch_comparison does -- see `just
-- warehouse` for the ordering.
--
-- READ THE `role` COLUMN BEFORE RANKING ANYTHING. On synthetic windows `toy_physical` is not a
-- competitor: it is the flat-plate model M3 *generated the labels with*, so it is marked
-- role='oracle' and winning is a tautology rather than a result. `baseline` marks the two naive
-- models that every model row must be read against. Ordering by mae_kwh without filtering on role
-- produces a leaderboard whose top entry means nothing.
--
-- Metrics are computed here rather than in Python so their definition is auditable in the dbt docs
-- site, and tests/dbt/test_forecast_reconciliation.py recomputes every row with the Python engine --
-- the same written-twice-and-pinned treatment the tariff and dispatch arithmetic get.
--
-- Grain: (site, target, model_key, window_start, window_end, horizon_hour).
with predictions as (
    select
        r.site,
        r.target,
        r.model_key,
        r.role,
        r.window_start,
        r.window_end,
        r.selection_rule_id,
        r.telemetry_lag_hours,
        r.training_data_source,
        r.config_hash,
        p.horizon_hour,
        p.p10_kwh,
        p.p50_kwh,
        p.p90_kwh,
        p.actual_kwh,
        -- An hour is scoreable only when both a prediction and an actual exist. A gap in either is
        -- a gap, never a zero: fabricating one here would flatter every model that failed to
        -- predict, which is exactly backwards.
        (p.p50_kwh is not null and p.actual_kwh is not null) as is_scoreable
    from {{ source('derived', 'forecast_runs') }} r
    join {{ source('derived', 'forecast_predictions') }} p on p.run_id = r.id
),

scored as (
    select
        *,
        abs(p50_kwh - actual_kwh) as absolute_error_kwh,
        p50_kwh - actual_kwh      as signed_error_kwh,
        -- Pinball loss: quantile * underprediction, or (quantile - 1) * overprediction. Mirrored
        -- exactly in energy_platform.forecasting.models.pinball_loss.
        case when actual_kwh >= p10_kwh then 0.10 * (actual_kwh - p10_kwh)
             else (0.10 - 1) * (actual_kwh - p10_kwh) end as pinball_p10,
        case when actual_kwh >= p50_kwh then 0.50 * (actual_kwh - p50_kwh)
             else (0.50 - 1) * (actual_kwh - p50_kwh) end as pinball_p50,
        case when actual_kwh >= p90_kwh then 0.90 * (actual_kwh - p90_kwh)
             else (0.90 - 1) * (actual_kwh - p90_kwh) end as pinball_p90,
        (p10_kwh is not null and p90_kwh is not null
            and actual_kwh between p10_kwh and p90_kwh) as inside_interval
    from predictions
    where is_scoreable
)

select
    site,
    target,
    model_key,
    role,
    window_start,
    window_end,
    horizon_hour,
    selection_rule_id,
    telemetry_lag_hours,
    training_data_source,
    config_hash,

    count(*)                                            as scored_hours,
    round(avg(absolute_error_kwh)::numeric, 6)          as mae_kwh,
    round(avg(signed_error_kwh)::numeric, 6)            as bias_kwh,
    round(sqrt(avg(power(signed_error_kwh, 2)))::numeric, 6) as rmse_kwh,

    -- Quantile losses are null for the models that emit a point forecast only (the baselines and
    -- the two physical models). Null rather than zero: a model that made no interval claim has not
    -- earned a perfect interval score.
    round(avg(pinball_p10)::numeric, 6)                 as pinball_p10_kwh,
    round(avg(pinball_p50)::numeric, 6)                 as pinball_p50_kwh,
    round(avg(pinball_p90)::numeric, 6)                 as pinball_p90_kwh,

    -- Reported, never claimed. The p10 fit is driven by a few hundred tail rows, so this is
    -- evidence about calibration rather than a promise of it; 0.80 is the target it should be read
    -- against.
    round(avg(case when inside_interval then 1.0 else 0.0 end)
        filter (where p10_kwh is not null)::numeric, 4) as interval_coverage_p10_p90

from scored
group by
    site, target, model_key, role, window_start, window_end, horizon_hour,
    selection_rule_id, telemetry_lag_hours, training_data_source, config_hash
