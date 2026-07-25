-- Open-Meteo weather actuals, pivoted from the long raw zone to one wide row per UTC hour.
-- Conditional aggregation is a clean pivot here: each (dataset, ts_utc) is unique in
-- observations_current, so max() is a pass-through, and an absent/missing interval collapses to
-- NULL -- gaps are preserved, never fabricated. mart_data_quality surfaces an all-NULL column.
select
    ts_utc,
    region,
    max(case when dataset = 'shortwave_radiation' then value end) as ghi_w_m2,
    max(case when dataset = 'direct_radiation'    then value end) as direct_radiation_w_m2,
    max(case when dataset = 'diffuse_radiation'   then value end) as diffuse_radiation_w_m2,
    max(case when dataset = 'temperature_2m'      then value end) as temperature_c,
    max(case when dataset = 'cloud_cover'         then value end) as cloud_cover_pct,
    max(case when dataset = 'wind_speed_10m'      then value end) as wind_speed_m_s,
    {{ berlin_calendar('ts_utc') }}
from {{ source('raw', 'observations_current') }}
where dataset in (
        'shortwave_radiation', 'direct_radiation', 'diffuse_radiation',
        'temperature_2m', 'cloud_cover', 'wind_speed_10m'
    )
    and resolution = 'hour'
group by ts_utc, region
