-- The M5/M6 foundation: energy balance and price for every hour in declared coverage, with UTC
-- and Europe/Berlin calendar columns, NULL wherever a source has a gap. A thin, materialised
-- projection of int_hourly_energy so downstream (tariff counterfactuals, dispatch optimiser)
-- reads a stable table.
select
    ts_utc,
    region,
    local_ts,
    local_date,
    local_hour,

    pv_production_kwh,
    household_load_kwh,
    battery_charge_kwh,
    battery_discharge_kwh,
    soc_frac,
    grid_import_kwh,
    grid_export_kwh,

    price_eur_mwh,
    balance_residual_kwh
from {{ ref('int_hourly_energy') }}
