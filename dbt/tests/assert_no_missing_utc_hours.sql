-- No hole within any contiguous Berlin day (per site): each (local_date, region) must contain
-- exactly as many rows as its UTC span implies (span in hours + 1). Grouping by (local_date,
-- region) isolates each day per site, so the seven-month void between the two disjoint coverage
-- windows is not flagged, while the DST days correctly require 23 / 25 (the October duplicate
-- wall-clock hour is two distinct ts_utc, so the span arithmetic still yields 25).
select
    local_date,
    region,
    count(*)                                                        as n,
    (extract(epoch from (max(ts_utc) - min(ts_utc))) / 3600)::int + 1 as span_hours
from {{ ref('mart_hourly_energy') }}
group by local_date, region
having count(*) <> (extract(epoch from (max(ts_utc) - min(ts_utc))) / 3600)::int + 1
