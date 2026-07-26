-- No UTC hour appears twice per site in the mart. The October fall-back's repeated local 02:00 is
-- two distinct ts_utc, so it is not a duplicate here -- this catches a genuinely doubled instant.
select
    ts_utc,
    region,
    count(*) as n
from {{ ref('mart_hourly_energy') }}
group by ts_utc, region
having count(*) > 1
