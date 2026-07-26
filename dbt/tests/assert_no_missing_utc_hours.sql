-- Every declared Berlin day must be complete, per site. The expectation is built from the
-- DECLARED coverage windows and the Berlin calendar -- never from the rows under test: deriving
-- it as `max(ts_utc) - min(ts_utc)` within the same group makes edge truncation invisible (drop
-- a day's first three hours and the span shrinks in lockstep with the count), and says nothing
-- about a day that is missing outright.
--
-- The hour count comes from the calendar arithmetic itself: 24 normally, 23 on the spring-forward
-- day, 25 on the fall-back day (whose repeated wall-clock 02:00 is two distinct ts_utc). Days
-- outside the declared windows are not expected, so the seven-month void between the two seeded
-- windows is correctly not flagged.
{% set coverage = var('coverage_windows') %}

with windows (start_date, end_date) as (
    values
    {%- for w in coverage %}
        (date '{{ w.start }}', date '{{ w.end }}'){{ "," if not loop.last }}
    {%- endfor %}
),

declared_days as (
    select generate_series(start_date, end_date, interval '1 day')::date as local_date
    from windows
),

sites as (
    select distinct region from {{ ref('mart_hourly_energy') }}
),

expected as (
    select
        d.local_date,
        s.region,
        (extract(epoch from (
            ((d.local_date + 1)::timestamp at time zone 'Europe/Berlin')
            - (d.local_date::timestamp at time zone 'Europe/Berlin')
        )) / 3600)::int as expected_n
    from declared_days d
    cross join sites s
),

actual as (
    select local_date, region, count(*) as n
    from {{ ref('mart_hourly_energy') }}
    group by local_date, region
),

-- An empty mart yields no sites, hence no expectations: without this it would pass by silence.
no_sites as (
    select null::date as local_date, '<mart is empty>' as region, 0 as expected_n, 0::bigint as n
    where not exists (select 1 from sites)
)

select e.local_date, e.region, e.expected_n, coalesce(a.n, 0) as n
from expected e
left join actual a
    on a.local_date = e.local_date and a.region = e.region
where coalesce(a.n, 0) <> e.expected_n
union all
select local_date, region, expected_n, n from no_sites
