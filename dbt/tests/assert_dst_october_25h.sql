-- Fall-back: the Europe/Berlin day 2024-10-27 has 25 local hours (local 02:00 occurs at two
-- distinct ts_utc). Returns a row (failure) unless mart_hourly_energy holds exactly 25.
select
    date '2024-10-27' as local_date,
    count(*)          as n
from {{ ref('mart_hourly_energy') }}
where local_date = date '2024-10-27'
having count(*) <> 25
