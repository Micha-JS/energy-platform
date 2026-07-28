-- Per-source coverage at MONTHLY grain: for each Europe/Berlin calendar month and each ingested
-- series, how many hours the pipeline declared it covers, how many actually arrived, and whether
-- the month is therefore partial.
--
-- WHY THIS EXISTS ALONGSIDE mart_data_quality. That model answers the same question over all time
-- at once -- one row per (source, dataset, region, resolution) -- which is the right shape for a
-- freshness panel and the wrong one for "show me coverage month by month". M9's dashboard is a
-- pure presentation layer: it may reshape a mart but never re-aggregate one, so the monthly grain
-- has to exist in the warehouse or not at all. This is that mart.
--
-- THE COLUMN VOCABULARY IS DELIBERATELY THE ECONOMICS MARTS'. expected_hours / covered_hours /
-- gap_hours / completeness_ratio / is_partial_month mean exactly what they mean in
-- mart_tariff_counterfactuals and mart_solar_economics, and expected_hours comes from the same
-- berlin_month_hours macro, so a reader who has learned the convention once has learned it here.
-- covered_hours comes from declared_coverage_hours(), the macro those marts already share.
--
-- THE THREE NUMBERS ANSWER THREE DIFFERENT QUESTIONS, which is the whole point of carrying all of
-- them: expected_hours is how long the month is, covered_hours is how much of it the pipeline
-- ever claimed, and present_hours is how much arrived. covered_hours = 0 with present_hours = 0
-- is "we never ran here"; covered_hours = 168 with present_hours = 0 is "we said we did and
-- nothing landed". Collapsing to a single ratio would make those indistinguishable.
--
-- completeness_ratio is against expected_hours, not covered_hours, for the same reason the
-- economics marts do it: the denominator a euro figure must be read against is the calendar
-- month, because that is what a monthly total looks like it means.
--
-- Hourly resolution only. expected_hours is an hour count, so a quarter-hourly series would be
-- measured against the wrong unit; those are reported by mart_data_quality, which is
-- resolution-aware and unit-free.

with declared as (
    {{ declared_coverage_hours() }}
),

-- Every series the raw zone has ever written, independent of month. Crossed with the months
-- below so a month in which a known series produced nothing is a row saying zero, not an absence
-- -- the same reason mart_data_quality drives from ingestion rather than from observations.
series as (
    select distinct
        i.source,
        o.dataset,
        o.region
    from {{ source('raw', 'observations_current') }} o
    join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
    where o.resolution = 'hour'
),

observed as (
    select
        date_trunc('month', (o.ts_utc at time zone 'Europe/Berlin')::date)::date as local_month,
        i.source,
        o.dataset,
        o.region,
        count(*) filter (where not o.is_missing) as present_hours,
        count(*) filter (where o.value is null)  as null_count,
        min(o.ts_utc)                            as min_ts_utc,
        max(o.ts_utc)                            as max_ts_utc
    from {{ source('raw', 'observations_current') }} o
    join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
    where o.resolution = 'hour'
    group by 1, 2, 3, 4
),

-- Declared months UNION observed months, not just declared: data outside every declared window
-- should not exist, and if it does the honest response is a visible row with covered_hours = 0
-- rather than a filter that hides it.
months as (
    select local_month from declared
    union
    select local_month from observed
),

grid as (
    select
        m.local_month,
        s.source,
        s.dataset,
        s.region
    from months m
    cross join series s
)

select
    g.local_month,
    g.source,
    g.dataset,
    g.region,
    {{ telemetry_data_mode('g.source') }}                     as data_mode,
    {{ berlin_month_hours('g.local_month') }}                 as expected_hours,
    coalesce(d.covered_hours, 0)                              as covered_hours,
    coalesce(o.present_hours, 0)                              as present_hours,
    coalesce(o.null_count, 0)                                 as null_count,
    {{ berlin_month_hours('g.local_month') }}
        - coalesce(o.present_hours, 0)                        as gap_hours,
    round(
        coalesce(o.present_hours, 0)::numeric
        / nullif({{ berlin_month_hours('g.local_month') }}, 0),
        6
    )                                                         as completeness_ratio,
    coalesce(o.present_hours, 0)
        < {{ berlin_month_hours('g.local_month') }}           as is_partial_month,
    o.min_ts_utc,
    o.max_ts_utc
from grid g
left join declared d
    on d.local_month = g.local_month
left join observed o
    on  o.local_month = g.local_month
    and o.source      = g.source
    and o.dataset     = g.dataset
    and o.region      = g.region
