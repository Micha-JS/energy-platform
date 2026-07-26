-- Open-Meteo weather actuals, pivoted from the long raw zone to one wide row per UTC hour and
-- source. Conditional aggregation is a clean pivot here: each (source, dataset, ts_utc) is unique
-- in observations_current, so max() is a pass-through, and an absent/missing interval collapses to
-- NULL -- gaps are preserved, never fabricated. mart_data_quality surfaces an all-NULL column.
-- `source` is carried and grouped on so a second weather provider could never blend into the same
-- row (observations_current keeps one row per source, not one row overall).
select
    i.source,
    o.ts_utc,
    o.region,
    max(case when o.dataset = 'shortwave_radiation' then o.value end) as ghi_w_m2,
    max(case when o.dataset = 'direct_radiation'    then o.value end) as direct_radiation_w_m2,
    max(case when o.dataset = 'diffuse_radiation'   then o.value end) as diffuse_radiation_w_m2,
    max(case when o.dataset = 'temperature_2m'      then o.value end) as temperature_c,
    max(case when o.dataset = 'cloud_cover'         then o.value end) as cloud_cover_pct,
    max(case when o.dataset = 'wind_speed_10m'      then o.value end) as wind_speed_m_s,
    {{ berlin_calendar('o.ts_utc') }}
from {{ source('raw', 'observations_current') }} o
join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
where o.dataset in (
        'shortwave_radiation', 'direct_radiation', 'diffuse_radiation',
        'temperature_2m', 'cloud_cover', 'wind_speed_10m'
    )
    and o.resolution = 'hour'
group by i.source, o.ts_utc, o.region
