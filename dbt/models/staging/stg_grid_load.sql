-- Total grid load / Netzlast (SMARD filter 410), MW.
select
    ts_utc,
    region,
    resolution,
    value as load_mw,
    {{ berlin_calendar('ts_utc') }}
from {{ source('raw', 'observations_current') }}
where dataset = 'grid_load'
