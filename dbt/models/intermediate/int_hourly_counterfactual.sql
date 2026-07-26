-- The hourly grid flows under each battery scenario, so the two can be priced side by side.
-- Grain: (ts_utc, region, scenario), scenario in {battery, no_battery} -- two rows per hour of
-- int_hourly_energy.
--
--   battery     -- what the meter actually saw: the telemetry's own import/export, with the
--                  battery running naive self-consumption.
--   no_battery  -- the counterfactual, derived from PV and load alone:
--                    import = max(load - pv, 0),  export = max(pv - load, 0)
--                  With no storage there is nowhere for energy to hide, so the AC-node identity
--                  pv + import == load + export holds exactly (asserted by a singular test).
--
-- NULL HANDLING IS LOAD-BEARING. Postgres `greatest(NULL, 0)` returns 0, NOT NULL -- the obvious
-- one-liner `greatest(household_load_kwh - pv_production_kwh, 0)` would silently fabricate a
-- zero-import hour wherever telemetry gapped, which is exactly the fabrication the raw zone
-- refuses to do. Hence the explicit null guard on every derived flow. Its Python twin,
-- energy_platform.tariffs.counterfactual.no_battery_flows, returns None for the same inputs.
--
-- Branches off int_hourly_energy (energy balance + price) rather than the spine, because pricing
-- needs both; a future non-energy mart should still branch off int_hourly_spine.

with flows as (
    select
        e.ts_utc,
        e.region,
        e.local_ts,
        e.local_date,
        e.local_hour,
        s.scenario,

        e.pv_production_kwh,
        e.household_load_kwh,
        e.price_eur_mwh,

        -- Only the battery scenario has a battery; NULL is the honest value for the other, not 0.
        case when s.scenario = 'battery' then e.battery_charge_kwh end    as battery_charge_kwh,
        case when s.scenario = 'battery' then e.battery_discharge_kwh end as battery_discharge_kwh,

        case s.scenario
            when 'battery' then e.grid_import_kwh
            when 'no_battery' then
                case
                    when e.pv_production_kwh is null or e.household_load_kwh is null then null
                    else greatest(e.household_load_kwh - e.pv_production_kwh, 0)
                end
        end as grid_import_kwh,

        case s.scenario
            when 'battery' then e.grid_export_kwh
            when 'no_battery' then
                case
                    when e.pv_production_kwh is null or e.household_load_kwh is null then null
                    else greatest(e.pv_production_kwh - e.household_load_kwh, 0)
                end
        end as grid_export_kwh

    from {{ ref('int_hourly_energy') }} e
    cross join (values ('battery'), ('no_battery')) as s (scenario)
)

select
    ts_utc,
    region,
    local_ts,
    local_date,
    local_hour,
    scenario,
    pv_production_kwh,
    household_load_kwh,
    price_eur_mwh,
    battery_charge_kwh,
    battery_discharge_kwh,
    grid_import_kwh,
    grid_export_kwh,

    -- Self-consumption, defined as the load served WITHOUT the grid: load - import.
    --
    -- The textbook alternative, pv - export, is equivalent whenever storage round-trips within the
    -- accounting period, and is what most datasheets quote. It is NOT used here, because the
    -- synthetic generator simulates each Berlin day independently (that is what makes it a pure
    -- function of (config, date), each partition idempotent), so SoC resets at every midnight and
    -- whatever the battery still held is discarded. pv - export would count that discarded energy
    -- as self-consumed; load - import cannot, because it only ever counts energy that actually
    -- reached the load.
    --
    -- It is also the measure the economics want: load - import is exactly the import that did not
    -- happen, which is what avoided_grid_cost_eur prices. All of it is PV-origin because the naive
    -- controller never charges from the grid; a grid-charging controller (M6 may introduce one)
    -- would need this revisited.
    case
        when household_load_kwh is null or grid_import_kwh is null then null
        else household_load_kwh - grid_import_kwh
    end as self_consumed_kwh

from flows
