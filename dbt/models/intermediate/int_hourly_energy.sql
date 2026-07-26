-- The hourly energy balance joined with price, on the declared spine. LEFT JOINs preserve gaps:
-- an hour missing in any source stays NULL, never filled. balance_residual_kwh is the AC-node
-- identity residual -- ~0 where telemetry is complete, NULL where any telemetry term is missing.
-- Weather actuals are intentionally not joined (the spec mart is energy + price); a future
-- weather mart should branch off int_hourly_spine rather than widening this model.
select
    s.ts_utc,
    s.region,
    s.local_ts,
    s.local_date,
    s.local_hour,

    t.pv_production_kwh,
    t.household_load_kwh,
    t.battery_charge_kwh,
    t.battery_discharge_kwh,
    t.soc_frac,
    t.grid_import_kwh,
    t.grid_export_kwh,

    p.price_eur_mwh,

    (t.pv_production_kwh + t.grid_import_kwh + t.battery_discharge_kwh)
        - (t.household_load_kwh + t.grid_export_kwh + t.battery_charge_kwh)
        as balance_residual_kwh

from {{ ref('int_hourly_spine') }} s
left join {{ ref('stg_telemetry') }} t
    on t.ts_utc = s.ts_utc and t.region = s.region
left join {{ ref('stg_prices') }} p
    on p.ts_utc = s.ts_utc
    and p.resolution = 'hour'
    -- Attach the national day-ahead price by explicit bidding zone, never by omission: without a
    -- region predicate a second ingested price region would fan out and double every spine row.
    and p.region = '{{ var("price_region", "DE") }}'
