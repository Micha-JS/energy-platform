-- Hourly money: what each scenario's grid flows cost (and earn) under each consumption tariff.
-- Grain: (ts_utc, region, scenario, tariff_id) -- the counterfactual flows cross-joined with the
-- consumption rows of the tariff catalogue.
--
-- The catalogue is a SEED (dbt/seeds/tariffs.csv), and that same file is what the Python engine
-- reads: one committed copy of every rate, two readers, so a parameter cannot drift between the
-- layers. The arithmetic is necessarily written twice -- here via the tariff_import_price_ct_kwh
-- macro, and in energy_platform.tariffs.engine -- and tests/dbt/test_tariff_reconciliation.py
-- recomputes every row below with the engine and asserts equality, so the formulas cannot drift
-- either.
--
-- Feed-in is NOT priced off the spot price. It is a flat statutory rate on a separate catalogue
-- row, so a negative-price hour still earns the full compensation; computing it as
-- `spot * export` would be a sign bug that a negative day-ahead hour makes expensive.
--
-- Hours stay unpriced rather than free: is_priced marks the rows where every term the cost needs
-- is present, and the marts aggregate only those. NULL in, NULL out.

with feed_in as (
    -- Exactly one compensation scheme applies at a time; selecting it by var (rather than joining
    -- every feed_in row) keeps the grain at one row per consumption tariff. A second scheme in
    -- the catalogue is a real possibility -- EEG rates change with commissioning date -- so the
    -- choice is explicit. Override with: dbt build --vars '{feed_in_tariff_id: eeg_2025}'
    select price_ct_kwh
    from {{ ref('tariffs') }}
    where kind = 'feed_in'
      and tariff_id = '{{ var("feed_in_tariff_id") }}'
),

consumption as (
    select *
    from {{ ref('tariffs') }}
    where kind in ('static', 'dynamic')
),

priced as (
    select
        c.ts_utc,
        c.region,
        c.local_ts,
        c.local_date,
        c.local_hour,
        c.scenario,
        t.tariff_id,
        t.kind as tariff_kind,

        c.pv_production_kwh,
        c.household_load_kwh,
        c.battery_charge_kwh,
        c.battery_discharge_kwh,
        c.grid_import_kwh,
        c.grid_export_kwh,
        c.self_consumed_kwh,
        c.price_eur_mwh,

        t.base_fee_eur_month,
        t.vat_rate,

        {{ tariff_import_price_ct_kwh('t', 'c.price_eur_mwh') }} as import_price_ct_kwh,
        f.price_ct_kwh as feed_in_price_ct_kwh

    from {{ ref('int_hourly_counterfactual') }} c
    cross join consumption t
    -- A scalar, single-row relation: CROSS JOIN cannot fan out, and an empty feed_in (a var
    -- naming a row that is not in the catalogue) empties the model loudly rather than silently
    -- pricing exports at zero.
    cross join feed_in f
)

select
    ts_utc,
    region,
    local_ts,
    local_date,
    local_hour,
    scenario,
    tariff_id,
    tariff_kind,

    pv_production_kwh,
    household_load_kwh,
    battery_charge_kwh,
    battery_discharge_kwh,
    grid_import_kwh,
    grid_export_kwh,
    self_consumed_kwh,
    price_eur_mwh,
    base_fee_eur_month,
    vat_rate,

    import_price_ct_kwh,
    feed_in_price_ct_kwh,

    -- ct/kWh * kWh -> ct, /100 -> EUR. The one place a rate becomes money.
    --
    -- Exact decimal arithmetic, cast back to double precision -- same reasoning as the
    -- tariff_price macro: computing in floating point leaves noise in the last digits that then
    -- accumulates through every sum in the marts. Telemetry is quantised to 1 Wh at emission, so
    -- casting the kWh columns to numeric loses nothing.
    (grid_import_kwh::numeric * import_price_ct_kwh::numeric / 100)::float8  as energy_cost_eur,
    (grid_export_kwh::numeric * feed_in_price_ct_kwh / 100)::float8          as feed_in_revenue_eur,

    -- What self-consumption was worth: the import that did not have to happen, valued at the
    -- price of the hour it did not happen in. For a dynamic tariff this is only meaningful hour
    -- by hour, which is why it is computed here and summed later, never from monthly averages.
    (self_consumed_kwh::numeric * import_price_ct_kwh::numeric / 100)::float8
        as avoided_grid_cost_eur,

    -- Every term the money columns need is present. The marts aggregate on this, so a gapped
    -- hour lowers the coverage ratio instead of quietly contributing zero.
    (
        grid_import_kwh is not null
        and grid_export_kwh is not null
        and self_consumed_kwh is not null
        and import_price_ct_kwh is not null
        and feed_in_price_ct_kwh is not null
    ) as is_priced

from priced
