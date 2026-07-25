-- Household telemetry (synthetic demo or real Fenecon), pivoted to one wide row per UTC hour,
-- energy in kWh. Accounted at the AC coupling point so the hourly node identity holds:
--   pv_production + grid_import + battery_discharge == household_load + grid_export + battery_charge
-- soc is the end-of-hour state of charge, a fraction in [soc_min, soc_max]. Absent intervals
-- collapse to NULL (gaps preserved).
select
    ts_utc,
    region,
    max(case when dataset = 'pv_production'     then value end) as pv_production_kwh,
    max(case when dataset = 'household_load'     then value end) as household_load_kwh,
    max(case when dataset = 'battery_charge'     then value end) as battery_charge_kwh,
    max(case when dataset = 'battery_discharge'  then value end) as battery_discharge_kwh,
    max(case when dataset = 'soc'                then value end) as soc_frac,
    max(case when dataset = 'grid_import'        then value end) as grid_import_kwh,
    max(case when dataset = 'grid_export'        then value end) as grid_export_kwh,
    {{ berlin_calendar('ts_utc') }}
from {{ source('raw', 'observations_current') }}
where dataset in (
        'pv_production', 'household_load', 'battery_charge', 'battery_discharge',
        'soc', 'grid_import', 'grid_export'
    )
    and resolution = 'hour'
group by ts_utc, region
