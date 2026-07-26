-- Total grid load / Netzlast (SMARD filter 410), MW. `source` is part of the grain for the same
-- reason as stg_prices: one row per source, never a blend of two providers.
select
    i.source,
    o.ts_utc,
    o.region,
    o.resolution,
    o.value as load_mw,
    {{ berlin_calendar('o.ts_utc') }}
from {{ source('raw', 'observations_current') }} o
join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
where o.dataset = 'grid_load'
