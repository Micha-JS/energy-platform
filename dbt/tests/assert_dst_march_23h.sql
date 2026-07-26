-- Spring-forward: the Europe/Berlin day 2024-03-31 has only 23 local hours. Returns a row
-- (failure) unless mart_hourly_energy holds exactly 23 per site. A count of 0 (window unseeded)
-- also fails, loudly, which is correct.
select
    region,
    date '2024-03-31' as local_date,
    count(*)          as n
from {{ ref('mart_hourly_energy') }}
where local_date = date '2024-03-31'
group by region
having count(*) <> 23
