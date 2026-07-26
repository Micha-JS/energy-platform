-- Day-ahead wholesale price (SMARD filter 4169), EUR/MWh. Both hourly and quarter-hour
-- resolutions are kept; downstream models select resolution = 'hour'. Day-ahead prices go
-- negative, so no lower clamp is applied here or in tests.
-- `source` is part of the grain: a second market connector (ENTSO-E) would publish the same
-- bidding zone at the same instant, and observations_current keeps one row per source.
select
    i.source,
    o.ts_utc,
    o.region,
    o.resolution,
    o.value as price_eur_mwh,
    {{ berlin_calendar('o.ts_utc') }}
from {{ source('raw', 'observations_current') }} o
join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
where o.dataset = 'day_ahead_price'
