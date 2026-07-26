-- Spring-forward: the Europe/Berlin day 2024-03-31 has only 23 local hours. Returns a row
-- (failure) unless mart_hourly_energy holds exactly 23 for every site.
--
-- The count is a FILTER over each site's rows, not a `where` + `having`: a bare
-- `where local_date = ... group by region having count(*) <> 23` produces zero groups when the
-- day is absent entirely, so the test would pass vacuously on an unseeded window or an empty
-- spine -- exactly the regression it exists to catch.
with per_site as (
    select
        region,
        count(*) filter (where local_date = date '2024-03-31') as n
    from {{ ref('mart_hourly_energy') }}
    group by region
),

-- ... and an empty mart has no sites at all, so it too must fail rather than pass by silence.
no_sites as (
    select '<mart is empty>' as region, 0::bigint as n
    where not exists (select 1 from per_site)
)

select region, date '2024-03-31' as local_date, n from per_site where n <> 23
union all
select region, date '2024-03-31' as local_date, n from no_sites
