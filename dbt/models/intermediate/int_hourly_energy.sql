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
    -- The M10 load split and the zone state. household_load_kwh stays the total every energy
    -- identity below is written in terms of: ac_power_kwh is a component of it, not a new node
    -- term, so balance_residual_kwh is deliberately unchanged by their arrival.
    t.load_base_kwh,
    t.ac_power_kwh,
    t.indoor_temp_c,
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
    on t.ts_utc = s.ts_utc
    and t.region = s.region
    -- One connector per site: two sources reporting the same site would otherwise double every
    -- spine row. See the telemetry_source_predicate macro for why the default is "no filter".
    and {{ telemetry_source_predicate('t') }}
left join {{ ref('stg_prices') }} p
    on p.ts_utc = s.ts_utc
    and p.resolution = 'hour'
    -- Attach the national day-ahead price by explicit bidding zone and provider, never by
    -- omission: without these predicates a second ingested price region -- or a second market
    -- connector (ENTSO-E) publishing the same zone -- would fan out and double every spine row.
    and p.region = '{{ var("price_region", "DE") }}'
    and p.source = '{{ var("price_source", "smard") }}'
