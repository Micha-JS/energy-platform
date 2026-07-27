-- M6's headline question: what would a perfectly-dispatched battery have cost, and how much would
-- that have saved? Grain: (window_start, window_end, region, tariff_id, scenario).
--
-- Four scenarios per window and tariff, all solved from the same hourly inputs, priced by the same
-- tariff engine, started from the same state of charge and settled with the same terminal
-- valuation -- so a difference between their totals is a difference in *dispatch* and nothing else:
--
--   no_battery         PV only. The M5 counterfactual, recomputed by the optimiser's own settler.
--   naive_telemetered  exactly what the M3 generator emitted -- see the warning below.
--   naive_continuous   the same naive policy, with state of charge carried across the window.
--   optimal            the MILP.
--
-- COMPARE AGAINST naive_continuous, NOT naive_telemetered. The generator simulates each Berlin day
-- independently -- that is what makes its output a pure function of (config, date) and re-ingestion
-- a verifiable no-op -- so state of charge resets at every midnight and whatever the battery still
-- held is discarded. Over the seeded March window it charges 54 kWh and discharges 13. An "optimum"
-- measured against that would be measuring the modelling simplification, not the optimisation.
-- naive_continuous runs the identical policy through the identical BatteryConfig and differs only
-- in carrying SoC through midnight; the gap between the two is battery_unreturned_kwh.
--
-- WHY optimal IS NEVER WORSE, as a theorem rather than an observation. no_battery and
-- naive_continuous each satisfy every constraint the optimiser solves under and start from the same
-- state of charge, so both are feasible points of its own problem and a minimum cannot exceed the
-- value at a feasible point. naive_telemetered is NOT feasible -- its SoC jumps at midnight -- and
-- is deliberately excluded from assert_optimal_never_costs_more_than_naive for exactly that reason.
--
-- adjusted_net_cost_eur IS THE ONLY COLUMN THE SCENARIOS MAY BE RANKED BY. It is the quantity the
-- optimiser minimises: net cost less the value of the energy still in the battery at the end.
-- Without that term hindsight optimisation drains the battery on the last evening and books the
-- proceeds as savings, which is an artefact of where the window stops. The adjustment is fully
-- exposed, and exactly reconstructible from the columns here:
--
--     terminal_value_eur = round(terminal_value_ct_kwh / 100
--                                * terminal_discharge_efficiency
--                                * terminal_soc_delta_kwh, 6)
--
-- so it can be undone (add it back to adjusted_net_cost_eur and net_cost_eur is what you get) or
-- recomputed at any other valuation by substituting a rate for terminal_value_ct_kwh, without
-- re-solving. terminal_discharge_efficiency is sqrt(round_trip_efficiency): a kWh in the store can
-- only ever reach the AC node through the discharge leg, so that is what it is worth, and it is in
-- this mart rather than left in the derived zone's battery jsonb precisely because the recipe above
-- is wrong without it -- omitting it overstates the credit by about 5% at the configured battery.
--
-- ENERGY COST ONLY: no standing charge. The Grundpreis is identical in all four scenarios and
-- cancels out of every savings figure, and a week is not a bill. mart_tariff_counterfactuals is
-- where a bill-shaped monthly number with a pro-rated base fee lives.
--
-- Coverage follows the convention mart_tariff_counterfactuals sets: expected_hours comes from the
-- Berlin calendar (167 across the spring-forward week, 169 across fall-back), never counted from
-- these rows, so a truncated window fails rather than passing by silence. There is no
-- covered_hours column here -- at window grain it would equal expected_hours by construction, and a
-- column that is always another column is noise.
--
-- Values are stable, hashes are not. The optimal schedule is non-unique, so this model asserts on
-- cost and on invariants and never on a specific hour's charge. See the `derived` source.

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
        r.solver,
        r.status                                  as solver_status,
        r.terminal_value_ct_kwh,
        r.terminal_soc_delta_kwh,
        r.terminal_discharge_efficiency,
        r.energy_cost_eur,
        r.feed_in_revenue_eur,
        r.net_cost_eur,
        r.terminal_value_eur,
        r.objective_eur                           as adjusted_net_cost_eur,
        r.battery_charge_kwh,
        r.battery_discharge_kwh,
        r.priced_hours,
        -- The expectation is the Berlin calendar's, not the solver's own report of what it saw.
        d.expected_hours
    from {{ source('derived', 'dispatch_runs') }} r
    -- INNER JOIN on purpose: a solve for a window the project no longer declares is stale output,
    -- not history. assert_dispatch_windows_are_declared fails on it rather than letting it be
    -- silently dropped here.
    join declared d
        on d.window_start = r.window_start
        and d.window_end = r.window_end
),

compared as (
    select
        runs.*,
        -- The baselines every row is measured against, broadcast across the scenarios of the same
        -- (window, site, tariff). A window function rather than a self-join: the grain is already
        -- one row per scenario, and a join could not fan out but could silently drop a scenario
        -- whose solve failed.
        max(adjusted_net_cost_eur) filter (where scenario = 'naive_continuous')
            over w                                as naive_continuous_cost_eur,
        max(adjusted_net_cost_eur) filter (where scenario = 'naive_telemetered')
            over w                                as naive_telemetered_cost_eur,
        max(adjusted_net_cost_eur) filter (where scenario = 'no_battery')
            over w                                as no_battery_cost_eur
    from runs
    window w as (partition by window_start, window_end, region, tariff_id)
)

select
    window_start,
    window_end,
    region,
    tariff_id,
    scenario,

    energy_cost_eur,
    feed_in_revenue_eur,
    net_cost_eur,

    -- What the window ended holding, relative to what it started with, and what that is worth. Zero
    -- for no_battery, which has no store whose level could change -- so its adjusted cost and its
    -- net cost are the same number, and the adjustment cannot flatter it.
    --
    -- The delta is the settled figure, not soc_end_kwh - soc_start_kwh: those two are persisted at
    -- the 1 Wh meter quantum, and subtracting them would leave the header's recipe reproducing
    -- terminal_value_eur only to within a Wh's worth of credit. The singular test
    -- assert_terminal_value_is_reconstructible holds all three factors to their product.
    terminal_soc_delta_kwh,
    terminal_value_ct_kwh,
    terminal_discharge_efficiency,
    terminal_value_eur,
    adjusted_net_cost_eur,

    battery_charge_kwh,
    battery_discharge_kwh,
    -- Charged but not returned within the window. A few percent of charge is honest round-trip
    -- loss; a large value is the generator's midnight reset, and is why naive_telemetered is not
    -- the baseline. Same definition as the column of this name in the M5 marts.
    battery_charge_kwh - battery_discharge_kwh    as battery_unreturned_kwh,

    -- How much cheaper this scenario was than each baseline. Positive means this dispatch saved
    -- money; the headline figure is the optimal row's savings_vs_naive_continuous_eur.
    naive_continuous_cost_eur - adjusted_net_cost_eur  as savings_vs_naive_continuous_eur,
    naive_telemetered_cost_eur - adjusted_net_cost_eur as savings_vs_naive_telemetered_eur,
    no_battery_cost_eur - adjusted_net_cost_eur        as savings_vs_no_battery_eur,

    -- Only meaningful against a baseline that actually cost something. A window whose baseline net
    -- cost is zero or negative (a well-exporting summer week) has no percentage saving, and
    -- reporting one would be arithmetic dressed up as a result.
    case
        when naive_continuous_cost_eur > 0
        then (naive_continuous_cost_eur - adjusted_net_cost_eur) / naive_continuous_cost_eur
    end                                           as savings_vs_naive_continuous_pct,

    solver,
    solver_status,

    expected_hours,
    priced_hours,
    expected_hours - priced_hours                 as gap_hours,
    priced_hours::numeric / expected_hours        as completeness_ratio,
    priced_hours < expected_hours                 as is_partial_window

from compared
