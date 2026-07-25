-- Day-ahead wholesale price (SMARD filter 4169), EUR/MWh. Both hourly and quarter-hour
-- resolutions are kept; downstream models select resolution = 'hour'. Day-ahead prices go
-- negative, so no lower clamp is applied here or in tests.
select
    ts_utc,
    region,
    resolution,
    value as price_eur_mwh,
    {{ berlin_calendar('ts_utc') }}
from {{ source('raw', 'observations_current') }}
where dataset = 'day_ahead_price'
