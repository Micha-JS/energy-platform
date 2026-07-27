-- What the sun earned: how much of the PV the household kept, how independent that made it, and
-- which was worth more -- consuming a kWh or selling it.
-- Grain: (local_month, region, scenario, tariff_id).
--
-- The scenario dimension is kept deliberately: the difference in self_consumption_rate between
-- battery and no_battery *is* what the battery does, and hiding it behind a single number would
-- throw the interesting comparison away. The tariff dimension is there because avoided_grid_cost
-- depends on what a kWh would have cost -- under a dynamic tariff, on the hour it would have cost
-- it, which is why that column is summed from hourly values and never derived from a monthly
-- average price.
--
-- Both rates share ONE numerator, self_consumed_kwh = load - import (see the intermediate for why
-- that definition and not pv - export): the self-consumption rate divides it by generation, the
-- autarky rate by consumption. Same numerator, two denominators, so the pair cannot tell
-- inconsistent stories. Rates are computed over the same priced-hour population as their
-- denominator, and nullif guards the zero-denominator cases -- a month with no PV has no
-- self-consumption *rate*, which is not the same as a rate of zero.
--
-- READ battery_unreturned_kwh BEFORE COMPARING SCENARIOS. The synthetic generator simulates each
-- Berlin day independently, so the battery's state of charge resets at every midnight and whatever
-- it still held is discarded. That energy was paid for and never served load, which makes the
-- battery scenario look worse than the hardware is -- in the seeded March window the battery
-- charges 54 kWh and discharges 13. The column is charge - discharge over the period: near zero on
-- data with continuous SoC, large wherever the day-boundary reset bites. It is NULL for
-- no_battery, which has no battery to be unreturned.
--
-- Coverage columns follow mart_tariff_counterfactuals exactly; see that model for the convention.

with declared as (
    {{ declared_coverage_hours() }}
),

monthly as (
    select
        date_trunc('month', local_date)::date                as local_month,
        region,
        scenario,
        tariff_id,
        max(tariff_kind)                                     as tariff_kind,

        count(*) filter (where is_priced)                    as priced_hours,

        sum(pv_production_kwh)      filter (where is_priced) as pv_production_kwh,
        sum(household_load_kwh)     filter (where is_priced) as household_load_kwh,
        sum(self_consumed_kwh)      filter (where is_priced) as self_consumed_kwh,
        sum(grid_import_kwh)        filter (where is_priced) as grid_import_kwh,
        sum(grid_export_kwh)        filter (where is_priced) as grid_export_kwh,
        sum(avoided_grid_cost_eur)  filter (where is_priced) as avoided_grid_cost_eur,
        sum(feed_in_revenue_eur)    filter (where is_priced) as feed_in_revenue_eur,

        sum(battery_charge_kwh)     filter (where is_priced) as battery_charge_kwh,
        sum(battery_discharge_kwh)  filter (where is_priced) as battery_discharge_kwh
    from {{ ref('int_hourly_tariff_cost') }}
    group by 1, 2, 3, 4
),

covered as (
    select
        m.*,
        {{ berlin_month_hours('m.local_month') }} as expected_hours,
        coalesce(d.covered_hours, 0)              as covered_hours
    from monthly m
    left join declared d on d.local_month = m.local_month
)

select
    local_month,
    region,
    scenario,
    tariff_id,
    tariff_kind,

    pv_production_kwh,
    household_load_kwh,
    self_consumed_kwh,
    grid_import_kwh,
    grid_export_kwh,

    -- Share of generation that served the load; share of load served without the grid.
    self_consumed_kwh / nullif(pv_production_kwh, 0)      as self_consumption_rate,
    self_consumed_kwh / nullif(household_load_kwh, 0)     as autarky_rate,

    battery_charge_kwh,
    battery_discharge_kwh,
    -- Charged but never given back within the period -- see the header. Round-trip losses put a
    -- few percent of charge here legitimately; anything larger is the day-boundary SoC reset.
    battery_charge_kwh - battery_discharge_kwh            as battery_unreturned_kwh,

    -- The two ways a kWh of sunshine pays: not buying it, or selling it. Under German tariffs the
    -- first is worth roughly four times the second, which is the whole economic case for storage.
    avoided_grid_cost_eur,
    feed_in_revenue_eur,
    avoided_grid_cost_eur + feed_in_revenue_eur           as solar_value_eur,

    expected_hours,
    covered_hours,
    priced_hours,
    expected_hours - priced_hours                         as gap_hours,
    priced_hours::numeric / expected_hours                as completeness_ratio,
    priced_hours < expected_hours                         as is_partial_month

from covered
