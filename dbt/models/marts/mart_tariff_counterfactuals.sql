-- The milestone's headline question: what would this household have paid, per month, under each
-- tariff, with and without the battery. Grain: (local_month, region, scenario, tariff_id).
--
-- COVERAGE IS PART OF THE ANSWER. The demo seeds two week-long windows, so no month in the
-- warehouse is complete, and a monthly total presented as if it were is a lie. Every row
-- therefore carries what it is built from:
--
--   expected_hours     -- the DST-correct length of the calendar month (743 in March, 745 in
--                         October), from the Berlin calendar, never counted from these rows
--   covered_hours      -- hours of that month inside the declared coverage_windows
--   priced_hours       -- hours actually priced (every term present)
--   gap_hours          -- expected_hours - priced_hours
--   completeness_ratio -- priced_hours / expected_hours, in [0, 1]
--   is_partial_month   -- the flag a consumer must not have to derive
--
-- The base fee is pro-rated by completeness_ratio for the same reason: a whole month's Grundpreis
-- against seven days of energy would make total_cost_eur incomparable between months. The full
-- monthly fee stays in base_fee_eur_month so the pro-rating can be undone.
--
-- Aggregates sum only priced hours, so a gap lowers the ratio rather than contributing zero.
--
-- READ battery_unreturned_kwh BEFORE CONCLUDING ANYTHING ABOUT THE BATTERY. The synthetic
-- generator simulates each Berlin day independently, so state of charge resets at every midnight
-- and whatever the battery still held is discarded. That energy was bought and never served load,
-- so the battery scenario carries its cost without its benefit -- in the seeded March window it is
-- 41 kWh, enough to make the battery look like a net loss under both tariffs. The column is
-- charge - discharge over the period: a few percent of charge is honest round-trip loss, anything
-- larger is the day-boundary reset. NULL for no_battery, which has no battery.

-- Hours of each month that the declared coverage windows actually claim, so a reader can
-- separate "the pipeline never covered this month" from "it covered it and the data gapped".
-- Shared with mart_solar_economics, which carries the same column.
with declared as (
    {{ declared_coverage_hours() }}
),

monthly as (
    select
        date_trunc('month', local_date)::date              as local_month,
        region,
        scenario,
        tariff_id,
        -- Constant within the group (one catalogue row per tariff); max() is simply the
        -- aggregate that lets a per-tariff constant through the group by.
        max(tariff_kind)                                  as tariff_kind,
        max(base_fee_eur_month)                           as base_fee_eur_month,
        max(vat_rate)                                     as vat_rate,

        count(*) filter (where is_priced)                 as priced_hours,

        sum(grid_import_kwh)     filter (where is_priced) as grid_import_kwh,
        sum(grid_export_kwh)     filter (where is_priced) as grid_export_kwh,
        sum(battery_charge_kwh)  filter (where is_priced) as battery_charge_kwh,
        sum(battery_discharge_kwh) filter (where is_priced) as battery_discharge_kwh,
        sum(energy_cost_eur)     filter (where is_priced) as energy_cost_eur,
        sum(feed_in_revenue_eur) filter (where is_priced) as feed_in_revenue_eur
    from {{ ref('int_hourly_tariff_cost') }}
    group by 1, 2, 3, 4
),

costed as (
    select
        m.*,
        {{ berlin_month_hours('m.local_month') }} as expected_hours,
        coalesce(d.covered_hours, 0)              as covered_hours,
        -- Pro-rated standing charge, grossed up by VAT exactly as the energy price is.
        m.base_fee_eur_month * (1 + m.vat_rate) * m.priced_hours
            / {{ berlin_month_hours('m.local_month') }} as base_fee_eur
    from monthly m
    left join declared d on d.local_month = m.local_month
)

select
    local_month,
    region,
    scenario,
    tariff_id,
    tariff_kind,

    grid_import_kwh,
    grid_export_kwh,
    -- Charged but not returned within the period -- see the header before comparing scenarios.
    battery_charge_kwh - battery_discharge_kwh            as battery_unreturned_kwh,

    energy_cost_eur,
    base_fee_eur,
    energy_cost_eur + base_fee_eur                        as total_cost_eur,
    feed_in_revenue_eur,
    energy_cost_eur + base_fee_eur - feed_in_revenue_eur  as net_cost_eur,

    base_fee_eur_month,

    expected_hours,
    covered_hours,
    priced_hours,
    expected_hours - priced_hours                         as gap_hours,
    priced_hours::numeric / expected_hours                as completeness_ratio,
    priced_hours < expected_hours                         as is_partial_month

from costed
