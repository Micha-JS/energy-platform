-- Fall-back: the Europe/Berlin day 2024-10-27 has 25 local hours (local 02:00 occurs at two
-- distinct ts_utc). Returns a row (failure) unless mart_hourly_energy holds exactly 25 for every
-- site. Structured like the March test: a FILTER-ed count per site plus an empty-mart sentinel,
-- so a missing day or an empty mart fails loudly instead of producing zero groups and passing.
with per_site as (
    select
        region,
        count(*) filter (where local_date = date '2024-10-27') as n
    from {{ ref('mart_hourly_energy') }}
    group by region
),

no_sites as (
    select '<mart is empty>' as region, 0::bigint as n
    where not exists (select 1 from per_site)
)

select region, date '2024-10-27' as local_date, n from per_site where n <> 25
union all
select region, date '2024-10-27' as local_date, n from no_sites
