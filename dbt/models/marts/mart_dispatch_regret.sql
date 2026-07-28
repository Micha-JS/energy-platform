-- M8's headline question, and the one this whole platform was built to answer: of the savings a
-- perfect battery schedule would have produced, how much did dispatch driven by *real forecasts*
-- actually capture? Grain: (window_start, window_end, region, tariff_id) -- one row per comparison,
-- not per scenario, because regret and captured share are relations *between* scenarios and a
-- column that only makes sense across rows belongs in a wide model.
--
-- Three costs, all `objective_eur` over the SAME simulated span, from the SAME starting state of
-- charge, settled at the SAME terminal rate -- see energy_platform.dispatch.forward. A difference
-- between them is a difference in dispatch and nothing else:
--
--   naive_continuous       M3's self-consumption policy, SoC carried. The baseline a household gets
--                          for free by owning the battery.
--   forecast_driven        day-ahead plans made at each Berlin midnight from M7 forecasts, executed
--                          against what actually happened. What this platform can really deliver.
--   perfect_foresight_plan the SAME rolling day-ahead controller, handed the actuals as its
--                          forecast. Not a deliverable -- a decomposition instrument.
--   optimal                the hindsight MILP over the same actuals. The ceiling nobody can reach.
--
-- REGRET DECOMPOSES, AND REPORTING IT UNDIVIDED WOULD MISATTRIBUTE IT. A day-ahead controller
-- commits one day at a time and cannot move energy across a midnight it has already passed; the
-- hindsight optimum can. So part of what forecast-driven dispatch gives up is nothing to do with
-- the forecast, and no model improvement could ever recover it:
--
--     regret_eur = forecast_error_cost_eur + myopia_cost_eur
--
-- myopia_cost_eur is what the perfect-foresight controller still loses to hindsight -- pure
-- consequence of the daily decision horizon. forecast_error_cost_eur is what imperfect forecasts
-- cost on top of that, and is the only part M7 could improve. On the seeded spring window the
-- myopia term is a real fraction of the total, which is exactly why it is a column and not a
-- footnote.
--
-- THE SPAN IS NOT THE WINDOW, and the columns say so. M7 fits nothing until min_train_days of
-- history has accumulated and then refits on a weekly stride, so the simulation starts later than
-- the window it belongs to -- sim_start/sim_end/simulated_days record what was actually covered,
-- and expected_hours is the Berlin calendar length of THAT span, never of the declared window and
-- never a count of these rows. A window with no fitted model at all is present with
-- is_simulated = false and a reason, rather than absent: a window that vanishes lets
-- assert_forward_dispatch_windows_are_declared pass over something nobody can see is missing, which
-- is exactly how the two DST weeks went unnoticed in mart_forecast_eval at M7.
--
-- WHAT IS A THEOREM AND WHAT IS NOT -- the most important comment in this file.
--
--   optimal <= naive_continuous    THEOREM. M6's guarantee: the naive policy is a feasible point of
--                                  the optimiser's own problem.
--   optimal <= forecast_driven     THEOREM. The executed trajectory obeys every constraint the
--                                  optimiser solves under and starts from the same SoC, so it too
--                                  is a feasible point. See dispatch/execution.py.
--   forecast_driven <= naive       NOT A THEOREM, and deliberately never asserted anywhere.
--
-- A forecast good enough to beat naive self-consumption is an empirical result about this data, not
-- a property of the system. A bad forecast can and should lose to naive -- planning to charge for
-- an evening peak on the strength of sunshine that never arrives costs real money that naive would
-- not have spent. So regret_eur is bounded below by zero (it is measured against the theorem) while
-- captured_value_share is NOT bounded below and is not clamped: a negative share is the honest
-- report of a forecast that made things worse, and a test suite or a CASE expression that hid it
-- would be suppressing the single most interesting output this mart can produce.
--
-- CAPTURED VALUE SHARE IS THE NUMBER TO QUOTE. A euro figure over a 60-day span is not comparable
-- to one over a year, or between a sunny window and a dark one; the share normalises by the size of
-- the prize and answers the question a reader actually has. It is bounded above by 1 -- exactly
-- because optimal <= forecast_driven -- so a value above 1 is not a good result, it is a bug, and
-- the accepted_range test says so.

with declared as (
    {{ declared_coverage_windows() }}
),

runs as (
    select
        r.window_start,
        r.window_end,
        r.region,
        r.tariff_id,
        r.scenario,
        r.is_simulated,
        r.not_simulated_reason,
        r.sim_start,
        r.sim_end,
        r.simulated_days,
        r.fallback_days,
        r.clipped_hours,
        r.objective_eur,
        r.net_cost_eur,
        r.battery_charge_kwh,
        r.battery_discharge_kwh,
        r.priced_hours,
        r.pv_model_key,
        r.load_model_key,
        r.decision_rule_id,
        r.price_publication_rule_id,
        r.selection_rule_id,
        r.training_data_source
    from {{ source('derived', 'forward_dispatch_runs') }} r
    -- INNER JOIN on purpose, exactly as mart_dispatch_comparison does it: a simulation of a window
    -- the project no longer declares is stale output, not history, and the singular test fails on
    -- it rather than letting this join drop it silently.
    join declared d
        on d.window_start = r.window_start
        and d.window_end = r.window_end
),

compared as (
    select
        window_start,
        window_end,
        region,
        tariff_id,
        -- Every scalar below is constant across the scenarios of one comparison, so max() is a
        -- pick rather than an aggregation. bool_or on is_simulated for the same reason.
        bool_or(is_simulated)                        as is_simulated,
        max(not_simulated_reason)                    as not_simulated_reason,
        max(sim_start)                               as sim_start,
        max(sim_end)                                 as sim_end,
        max(simulated_days)                          as simulated_days,
        max(fallback_days)                           as fallback_days,
        max(pv_model_key)                            as pv_model_key,
        max(load_model_key)                          as load_model_key,
        max(decision_rule_id)                        as decision_rule_id,
        max(price_publication_rule_id)               as price_publication_rule_id,
        max(selection_rule_id)                       as selection_rule_id,
        max(training_data_source)                    as training_data_source,

        -- The three costs, pivoted. FILTER rather than a self-join: the grain is already one row
        -- per scenario, and a join could not fan out but could silently drop a scenario whose
        -- simulation failed -- which is the case this mart most needs to make visible.
        max(objective_eur) filter (where scenario = 'naive_continuous')  as naive_cost_eur,
        max(objective_eur) filter (where scenario = 'forecast_driven')   as forecast_driven_cost_eur,
        max(objective_eur) filter (where scenario = 'perfect_foresight_plan')
            as perfect_foresight_cost_eur,
        max(objective_eur) filter (where scenario = 'optimal')           as hindsight_cost_eur,

        max(clipped_hours) filter (where scenario = 'forecast_driven')   as clipped_hours,
        max(battery_charge_kwh) filter (where scenario = 'forecast_driven')
            as forecast_driven_charge_kwh,
        max(battery_discharge_kwh) filter (where scenario = 'forecast_driven')
            as forecast_driven_discharge_kwh,
        max(priced_hours) filter (where scenario = 'forecast_driven')    as priced_hours,
        count(*)                                                         as scenario_rows
    from runs
    group by window_start, window_end, region, tariff_id
)

select
    window_start,
    window_end,
    region,
    tariff_id,

    is_simulated,
    not_simulated_reason,
    sim_start,
    sim_end,
    simulated_days,
    -- Days inside the span the planner could not produce a usable forecast for, and fell back to
    -- naive self-consumption on. A large value means the comparison is measuring the fallback.
    fallback_days,
    -- Hours the plan asked for more than the battery could do. Not an error -- it is the recourse
    -- policy working -- but a high count means the plans were systematically infeasible.
    clipped_hours,

    naive_cost_eur,
    forecast_driven_cost_eur,
    perfect_foresight_cost_eur,
    hindsight_cost_eur,

    -- The total shortfall against perfect information. Non-negative by the theorem above, to within
    -- the settlement rounding the singular test budgets for.
    forecast_driven_cost_eur - hindsight_cost_eur    as regret_eur,
    -- ...and its two parts. The split is the difference between "our forecasts cost EUR X" and
    -- "deciding a day at a time costs EUR Y and our forecasts cost EUR X on top", which are very
    -- different claims about where the remaining value is.
    forecast_driven_cost_eur - perfect_foresight_cost_eur as forecast_error_cost_eur,
    perfect_foresight_cost_eur - hindsight_cost_eur       as myopia_cost_eur,
    -- The size of the prize: what perfect foresight would have been worth over naive dispatch.
    -- May be zero or negative-to-rounding on a window where the battery is PV-saturated and there
    -- is nothing left to optimise -- M6's central finding, and the reason for the guard below.
    naive_cost_eur - hindsight_cost_eur              as available_savings_eur,
    -- What forecast-driven dispatch actually delivered over naive. May be negative.
    naive_cost_eur - forecast_driven_cost_eur        as realised_savings_eur,

    -- THE HEADLINE. Fraction of the naive -> optimal gap that forecast-driven dispatch captured.
    -- Null where there was no gap to capture: dividing by a prize of zero produces a number with
    -- no meaning, and reporting one would be arithmetic dressed up as a result -- the same guard
    -- and the same reasoning as savings_vs_naive_continuous_pct in mart_dispatch_comparison.
    -- Deliberately NOT clamped below zero. See the header.
    case
        when naive_cost_eur - hindsight_cost_eur > 1e-6
        then (naive_cost_eur - forecast_driven_cost_eur)
             / (naive_cost_eur - hindsight_cost_eur)
    end                                              as captured_value_share,

    -- The same question asked of the controller rather than of the forecast: what share would a
    -- day-ahead planner with perfect information have captured? The ceiling on captured_value_share
    -- that no forecasting work could lift, and the honest thing to compare the headline against.
    case
        when naive_cost_eur - hindsight_cost_eur > 1e-6
        then (naive_cost_eur - perfect_foresight_cost_eur)
             / (naive_cost_eur - hindsight_cost_eur)
    end                                              as attainable_value_share,

    forecast_driven_charge_kwh,
    forecast_driven_discharge_kwh,

    pv_model_key,
    load_model_key,
    decision_rule_id,
    price_publication_rule_id,
    selection_rule_id,
    training_data_source,

    -- Coverage, on the simulated span rather than the declared window. Null-safe: an unsimulated
    -- window has no span to expect hours of, and a zero here would read as "expected nothing and
    -- got it" rather than "was never run".
    case when is_simulated then {{ berlin_span_hours('sim_start', 'sim_end') }} end
                                                     as expected_hours,
    priced_hours,
    case when is_simulated
         then {{ berlin_span_hours('sim_start', 'sim_end') }} - priced_hours
    end                                              as gap_hours,
    case when is_simulated
         then priced_hours::numeric / {{ berlin_span_hours('sim_start', 'sim_end') }}
    end                                              as completeness_ratio,
    case when is_simulated
         then priced_hours < {{ berlin_span_hours('sim_start', 'sim_end') }}
    end                                              as is_partial_window,
    scenario_rows

from compared
