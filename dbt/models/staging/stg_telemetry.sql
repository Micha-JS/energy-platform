-- Household telemetry (synthetic demo or real Fenecon), pivoted to one wide row per UTC hour and
-- source, energy in kWh. Accounted at the AC coupling point so the hourly node identity holds:
--   pv_production + grid_import + battery_discharge == household_load + grid_export + battery_charge
-- soc is the end-of-hour state of charge, a fraction in [soc_min, soc_max]. Absent intervals
-- collapse to NULL (gaps preserved).
--
-- From M10, consumption is separable into a second identity that assert_load_split_identity
-- enforces wherever all three are present:
--   household_load == load_base + ac_power
-- The air conditioner is a COMPONENT of household_load, not an extra term in the node identity
-- above -- so the balance is untouched by the split, and household_load remains the canonical
-- total. Both components are NULL for a producer that cannot separate them: the real Fenecon
-- meters the house, not the appliance, so unless a separate AC meter is mapped they simply do not
-- arrive. That is why the identity test is scoped to rows where all three are present rather than
-- being a not-null constraint. indoor_temperature is a state in degrees Celsius, not a flow --
-- like soc_frac, it sits in this model without participating in any energy identity.
--
-- `source` is part of the grain, not dropped: observations_current keeps the latest ingestion per
-- (source, dataset, region, resolution, partition_date), so two connectors reporting the same site
-- (synthetic demo and the real Fenecon both default to region = site_id) each contribute their own
-- rows. Grouping without `source` would silently blend them into a per-column max() of two
-- independent series. Downstream picks one with the `telemetry_source` var.
select
    i.source,
    o.ts_utc,
    o.region,
    max(case when o.dataset = 'pv_production'     then o.value end) as pv_production_kwh,
    max(case when o.dataset = 'household_load'     then o.value end) as household_load_kwh,
    max(case when o.dataset = 'load_base'          then o.value end) as load_base_kwh,
    max(case when o.dataset = 'ac_power'           then o.value end) as ac_power_kwh,
    max(case when o.dataset = 'indoor_temperature' then o.value end) as indoor_temp_c,
    max(case when o.dataset = 'battery_charge'     then o.value end) as battery_charge_kwh,
    max(case when o.dataset = 'battery_discharge'  then o.value end) as battery_discharge_kwh,
    max(case when o.dataset = 'soc'                then o.value end) as soc_frac,
    max(case when o.dataset = 'grid_import'        then o.value end) as grid_import_kwh,
    max(case when o.dataset = 'grid_export'        then o.value end) as grid_export_kwh,
    {{ berlin_calendar('o.ts_utc') }}
from {{ source('raw', 'observations_current') }} o
join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
where o.dataset in ({{ telemetry_datasets() }})
    and o.resolution = 'hour'
group by i.source, o.ts_utc, o.region
