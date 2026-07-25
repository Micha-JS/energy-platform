-- No UTC hour appears twice in the mart. The October fall-back's repeated local 02:00 is two
-- distinct ts_utc, so it is not a duplicate here -- this catches a genuinely doubled instant.
select
    ts_utc,
    count(*) as n
from {{ ref('mart_hourly_energy') }}
group by ts_utc
having count(*) > 1
